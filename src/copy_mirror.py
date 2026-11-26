"""COPY_MODE=mirror: an exact, scaled replica of one trader's perp book (paper only).

The position-copy mode (``copy_trade``) trades like a real follower would: late,
at its own prices, and never below the $10 minimum. That's the right test of
execution, but it can't reproduce the leader's results exactly.

This mode reproduces them. It replays **every fill** the leader makes, in
execution order, scaled by one fixed ratio set at startup::

    ratio = COPY_SCALE * INITIAL_EQUITY / leader_equity_at_start

Each mirrored fill uses the leader's own price and the leader's own fee times
the ratio; funding is the leader's actual payments times the ratio. There is
no minimum order size and no rounding. So from the moment the mirror starts:

    mirror P&L  ==  leader trading P&L * ratio
    mirror P&L% ==  leader trading P&L as a % of their starting equity

Deposits and withdrawals by the leader don't move the mirror (they aren't
trading). Positions the leader already holds at startup are joined at the
current mark, so P&L counts from that moment.

Self-healing: every fill carries ``startPosition``, the leader's size before
it. If that doesn't match the size the mirror has tracked (a fill missed during
downtime, or already in the startup snapshot), the mirror first resyncs to
``startPosition * ratio`` at that fill's price, logs ``mirror_resync``, then
applies the fill. A periodic check against the leader's live positions catches
anything the fill feed hasn't delivered.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import Settings
from .copy_trade import EPS, CopyClient, CopyPaperBroker, LeaderBook

STATE_FILE = "mirror_state.json"
CHECK_EVERY = 12  # ticks between leader-book checks (positions, marks, funding)
MISMATCH_CHECKS = 3  # consecutive book mismatches before forcing a resync
SEEN_WINDOW_MS = 120_000  # how long fill keys are remembered for de-duplication


def _is_perp(coin: str) -> bool:
    return not coin.startswith("@") and "/" not in coin


def fill_key(f: dict[str, Any]) -> str:
    return f"{f.get('tid')}:{f.get('time')}:{f.get('oid')}"


def signed_size(f: dict[str, Any]) -> float:
    sz = float(f["sz"])
    return sz if f["side"] == "B" else -sz


class MirrorTracker:
    def __init__(self, settings: Settings, broker: CopyPaperBroker, client: Optional[CopyClient] = None) -> None:
        if not settings.copy_leader:
            raise ValueError("COPY_MODE=mirror needs COPY_LEADER=0x... in .env")
        self.settings = settings
        self.broker = broker
        self.client = client or CopyClient(settings)
        self.client.info.WEIGHTS = {**self.client.info.WEIGHTS, "userFunding": 20}
        self.leader_address = settings.copy_leader.lower()
        self.ratio = 0.0
        self.tracked: dict[str, float] = {}  # leader size per coin, as the mirror believes it
        self.last_fill_ms = 0
        self.last_funding_ms = 0
        self.seen: dict[str, int] = {}  # fill key -> time, for de-duplication at page edges
        self.mismatch: dict[str, int] = {}
        self.started_at = ""
        self.leader_equity = 0.0
        self.leader_equity_start = 0.0
        self.ticks = 0
        self.fills_mirrored = 0
        self.resyncs = 0
        self.changed = False  # a fill or resync this tick: show status now
        self._statuses: list[str] = []
        self._state_path = Path(settings.logs_dir) / STATE_FILE
        self._load()

    # -- persistence --
    def _load(self) -> None:
        if not self._state_path.exists():
            return
        s = json.loads(self._state_path.read_text(encoding="utf-8"))
        if s.get("leader") != self.leader_address:
            self._state_path.rename(self._state_path.with_suffix(f".{int(time.time())}.bak"))
            return
        self.broker.load_state(s.get("broker", {}))
        self.ratio = float(s["ratio"])
        self.tracked = {c: float(v) for c, v in s.get("tracked", {}).items()}
        self.last_fill_ms = int(s.get("last_fill_ms", 0))
        self.last_funding_ms = int(s.get("last_funding_ms", 0))
        self.seen = {k: int(v) for k, v in s.get("seen", {}).items()}
        self.started_at = s.get("started_at", "")
        self.leader_equity_start = float(s.get("leader_equity_start", 0))
        self.fills_mirrored = int(s.get("fills_mirrored", 0))
        self.resyncs = int(s.get("resyncs", 0))

    def _save(self) -> None:
        state = {
            "leader": self.leader_address,
            "ratio": self.ratio,
            "broker": self.broker.to_state(),
            "tracked": self.tracked,
            "last_fill_ms": self.last_fill_ms,
            "last_funding_ms": self.last_funding_ms,
            "seen": self.seen,
            "started_at": self.started_at,
            "leader_equity_start": self.leader_equity_start,
            "fills_mirrored": self.fills_mirrored,
            "resyncs": self.resyncs,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        tmp.replace(self._state_path)

    # -- leader data --
    def _book(self) -> LeaderBook:
        self.client.new_tick()
        book = self.client.leader(self.leader_address)
        self.leader_equity = book.equity
        return book

    def _new_fills(self) -> list[dict[str, Any]]:
        """Fills since ``last_fill_ms``, in execution order, de-duplicated.

        The API returns fills oldest first and at most 2000 per call; the start
        time is inclusive, so the boundary fills come back again and are dropped
        by key. Order is kept as returned: trade ids are not sequential within a
        millisecond, so sorting by them would scramble execution order.
        """
        out: list[dict[str, Any]] = []
        start = self.last_fill_ms
        while True:
            page = self.client.info.post({"type": "userFillsByTime", "user": self.leader_address, "startTime": start})
            fresh = [f for f in page if fill_key(f) not in self.seen]
            for f in fresh:
                self.seen[fill_key(f)] = int(f["time"])
            out.extend(fresh)
            if len(page) < 2000 or not fresh:
                break
            start = max(int(f["time"]) for f in page)
        if out:
            self.last_fill_ms = max(self.last_fill_ms, max(int(f["time"]) for f in out))
        horizon = self.last_fill_ms - SEEN_WINDOW_MS
        self.seen = {k: t for k, t in self.seen.items() if t >= horizon}
        return out

    # -- mirroring --
    def _bootstrap(self, book: LeaderBook) -> list[str]:
        now = int(time.time() * 1000)
        self.ratio = self.settings.copy_scale * self.settings.initial_equity / book.equity if book.equity > 0 else 0.0
        self.leader_equity_start = book.equity
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.last_fill_ms = now
        self.last_funding_ms = now
        notes = []
        for coin, size in sorted(book.positions.items()):
            mark = book.details.get(coin, {}).get("value", 0.0) / abs(size) if size else 0.0
            if mark <= 0:
                mark = self.client.ctx(coin).mid
            self.broker.fill(coin, size * self.ratio, mark, 0.0, "mirror_start", fee=0.0)
            self.tracked[coin] = size
            notes.append(f"{coin} joined {size * self.ratio:+.8g} @ {mark:g} (leader {size:+g})")
        self.broker.log_event(
            "mirror_start",
            {"ratio": self.ratio, "leader_equity": book.equity, "positions": dict(book.positions)},
        )
        return notes

    def _resync(self, coin: str, leader_size: float, px: float, why: str) -> None:
        ours = self.broker.positions.get(coin)
        delta = leader_size * self.ratio - (ours.size if ours else 0.0)
        if abs(delta) > EPS:
            self.broker.fill(coin, delta, px, 0.0, "mirror_resync", fee=0.0)
        self.broker.log_event(
            "mirror_resync",
            {"symbol": coin, "tracked": self.tracked.get(coin, 0.0), "leader": leader_size, "price": px, "why": why},
        )
        self.resyncs += 1
        if abs(leader_size) > EPS:
            self.tracked[coin] = leader_size
        else:
            self.tracked.pop(coin, None)

    def apply_fill(self, f: dict[str, Any]) -> Optional[str]:
        coin = str(f.get("coin", ""))
        if not _is_perp(coin):
            return None
        px = float(f["px"])
        start = float(f["startPosition"])
        if abs(start - self.tracked.get(coin, 0.0)) > 1e-9 * max(1.0, abs(start)):
            self._resync(coin, start, px, "fill startPosition differs from tracked size")
        size = signed_size(f)
        fee = float(f.get("fee", 0) or 0) * self.ratio
        closed = self.broker.fill(coin, size * self.ratio, px, 0.0, "mirror", fee=fee)
        end = round(start + size, 10)
        if abs(end) > EPS:
            self.tracked[coin] = end
        else:
            self.tracked.pop(coin, None)
        self.fills_mirrored += 1
        self.broker.log_event(
            "mirror_fill",
            {
                "symbol": coin,
                "dir": f.get("dir"),
                "leader_size": size,
                "leader_px": px,
                "leader_fee": float(f.get("fee", 0) or 0),
                "leader_closed_pnl": float(f.get("closedPnl", 0) or 0),
                "size": size * self.ratio,
                "fee": fee,
                "liquidation": bool(f.get("liquidation")),
                "leader_time": int(f["time"]),
            },
        )
        line = f"{coin} {f.get('dir')} {size * self.ratio:+.8g} @ {px:g} (leader {size:+g})"
        if closed:
            line += f" closed pnl {sum(t.pnl for t in closed):+.4f}"
        return line

    def _funding(self) -> list[str]:
        entries = self.client.info.post(
            {"type": "userFunding", "user": self.leader_address, "startTime": self.last_funding_ms + 1}
        ) or []
        notes = []
        for e in entries:
            d = e.get("delta", {})
            coin = d.get("coin", "")
            amount = float(d.get("usdc", 0) or 0) * self.ratio
            self.broker.add_funding(coin, amount)
            notes.append(f"{coin} funding {amount:+.6f}")
            self.last_funding_ms = max(self.last_funding_ms, int(e.get("time", 0)))
        if notes:
            self.broker.log_event("mirror_funding", {"payments": notes})
        return notes

    def _check(self, book: LeaderBook) -> list[str]:
        """Compare against the leader's live book; mark to market at the leader's prices."""
        notes = []
        for coin in sorted(set(book.positions) | set(self.tracked)):
            live, ours = book.positions.get(coin, 0.0), self.tracked.get(coin, 0.0)
            if abs(live - ours) <= 1e-9 * max(1.0, abs(live)):
                self.mismatch.pop(coin, None)
                continue
            # The fill feed can lag the book by a moment: only act if it persists.
            self.mismatch[coin] = self.mismatch.get(coin, 0) + 1
            if self.mismatch[coin] >= MISMATCH_CHECKS:
                px = self._mark(book, coin)
                self._resync(coin, live, px, "leader book differs from tracked size")
                self.mismatch.pop(coin, None)
                notes.append(f"{coin} resynced to leader {live:+g}")
        self.broker.mark({c: self._mark(book, c) for c in self.broker.positions})
        return notes

    def _mark(self, book: LeaderBook, coin: str) -> float:
        size = book.positions.get(coin)
        value = book.details.get(coin, {}).get("value", 0.0)
        if size and value:
            return value / abs(size)
        return self.client.ctx(coin).mid

    def tick(self, paused: bool) -> list[str]:
        self.ticks += 1
        self.changed = False
        before = (self.fills_mirrored, self.resyncs)
        statuses: list[str] = []
        book: Optional[LeaderBook] = None
        if self.ratio <= 0:
            book = self._book()
            statuses += self._bootstrap(book)

        for f in self._new_fills():
            line = self.apply_fill(f)
            if line:
                print(f"[{datetime.now(timezone.utc).isoformat()}] MIRROR {line}")
                statuses.append(line)

        if book is not None or self.ticks % CHECK_EVERY == 0:
            book = book or self._book()
            statuses += self._funding()
            statuses += self._check(book)
        self._save()

        if paused:
            statuses.append("paused flag ignored: the mirror tracks the leader exactly")
        pnl = self.broker.equity - self.settings.initial_equity
        pct = pnl / self.settings.initial_equity * 100 if self.settings.initial_equity else 0.0
        statuses.append(
            f"mirror P&L {pnl:+.4f} ({pct:+.4f}%) = leader trading P&L {pnl / self.ratio if self.ratio else 0:+,.2f} "
            f"since {self.started_at[:19]}Z | fills {self.fills_mirrored} resyncs {self.resyncs}"
        )
        for coin, size in sorted(self.tracked.items()):
            ours = self.broker.positions.get(coin)
            statuses.append(f"{coin} leader {size:+g} me {ours.size if ours else 0:+.8g}")
        self.changed = (self.fills_mirrored, self.resyncs) != before
        self._statuses = statuses
        return statuses

    def heartbeat_lines(self, paused: bool = False) -> list[str]:
        head = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}] STATUS equity ${self.broker.equity:,.2f}"
        return [head] + [f"  {s}" for s in self._statuses]

    def summary(self) -> dict[str, Any]:
        return {
            "mode": "mirror",
            "leader": self.leader_address,
            "ratio": self.ratio,
            "leader_equity": round(self.leader_equity, 2),
            "leader_equity_start": round(self.leader_equity_start, 2),
            "started_at": self.started_at,
            "fills_mirrored": self.fills_mirrored,
            "resyncs": self.resyncs,
        }
