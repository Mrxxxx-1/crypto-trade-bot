"""STRATEGY=copy: mirror one Hyperliquid trader's perp positions, on paper or testnet.

Every ``COPY_POLL_SECONDS`` the bot reads the leader's positions on **every perp
dex** (the core book plus each HIP-3 builder dex such as ``xyz:`` stocks) and,
for each coin whose position *changed* since the last poll, trades our paper
account to the proportional target::

    target = leader_size * COPY_SCALE * (our_equity / leader_equity)

Why "on change" and not "always match the target": both equities drift with
every price tick, so chasing the exact ratio would trade constantly. Reacting
only when the leader acts reproduces their trades one for one; reductions,
closes and flips always follow, whether or not the bot is paused.

Rules that keep the copy honest:

- A coin we hold that the leader doesn't is closed immediately ("orphan"), so
  a restart or a missed poll can't leave us exposed to something they aren't.
- Orders below ``COPY_MIN_NOTIONAL`` (Hyperliquid's $10 minimum) are skipped
  and logged, exactly as a live order would be rejected. Small accounts
  therefore miss the leader's smallest positions -- see the README.
- Increases respect ``MAX_LEVERAGE`` (total notional / equity) and, when
  ``COPY_RISK_GUARDS=true``, the risk halts. Neither ever blocks a reduction.

Paper fills happen at the dex's mid price plus ``SLIPPAGE_BPS``, pay
``COPY_TAKER_FEE_BPS`` (``COPY_HIP3_FEE_BPS`` on builder dexes) and accrue
hourly funding at the live rate. Liquidation is not simulated.

``MODE=live`` with ``HL_TESTNET=true`` sends real IOC orders to testnet through
``TestnetExecutor`` (see the README). Mainnet copy trading is deliberately
unsupported: ``FuturesBot`` refuses ``STRATEGY=copy`` with ``MODE=live`` on mainnet.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import requests

from . import log_hygiene
from .config import Settings
from .leader_scan import INFO_URL as MAINNET_INFO_URL
from .leader_scan import STABLES, InfoClient
from .models import TradeResult

TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"

STATE_FILE = "copy_state.json"
EPS = 1e-12


def dex_of(coin: str) -> str:
    """``xyz:NVDA`` -> ``xyz``; core coins -> ``""``."""
    return coin.split(":", 1)[0] if ":" in coin else ""


def _sign(x: float) -> int:
    return (x > EPS) - (x < -EPS)


def round_size(size: float, decimals: int) -> float:
    """Round to the asset's size precision (nearest, keeping the sign)."""
    factor = 10 ** decimals
    return round(size * factor) / factor


# --------------------------------------------------------------------- leader


@dataclass
class LeaderBook:
    equity: float
    positions: dict[str, float]  # coin -> signed size
    details: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class AssetCtx:
    mid: float
    oracle: float
    funding: float  # hourly rate
    sz_decimals: int


class CopyClient:
    """Account positions across perp dexes, plus per-dex market data, for one network.

    Used twice in testnet mode: once on mainnet for the leader, once on testnet
    for our own account and execution prices. ``dexes`` pins the dex list
    (testnet lists hundreds of throwaway dexes; we only want the leader's).
    """

    def __init__(
        self,
        settings: Settings,
        info: Optional[InfoClient] = None,
        url: str = MAINNET_INFO_URL,
        dexes: Optional[list[str]] = None,
    ) -> None:
        self.settings = settings
        self.info = info or InfoClient(budget=900, url=url)
        self.info.WEIGHTS = {**self.info.WEIGHTS, "metaAndAssetCtxs": 20, "perpDexs": 20}
        self._fixed_dexes = dexes
        self._dexes: list[str] = []
        self._dexes_at = 0.0
        self._ctx: dict[str, dict[str, AssetCtx]] = {}

    def all_dexes(self) -> list[str]:
        raw = self.info.post({"type": "perpDexs"}) or []
        return [""] + [d["name"] for d in raw if d and d.get("name")]

    def dexes(self) -> list[str]:
        if self._fixed_dexes is not None:
            return self._fixed_dexes
        wanted = list(self.settings.copy_dexes)
        if wanted != ["all"]:
            return ["" if d in ("core", "main") else d for d in wanted]
        if not self._dexes or time.time() - self._dexes_at > 3600:
            self._dexes = self.all_dexes()
            self._dexes_at = time.time()
        return self._dexes

    def leader(self, address: str) -> LeaderBook:
        """Positions and equity of ``address`` on every dex in ``dexes()``."""
        equity = 0.0
        positions: dict[str, float] = {}
        details: dict[str, dict[str, Any]] = {}
        for dex in self.dexes():
            body: dict[str, Any] = {"type": "clearinghouseState", "user": address}
            if dex:
                body["dex"] = dex
            st = self.info.post(body)
            equity += float(st["marginSummary"]["accountValue"])
            for ap in st.get("assetPositions", []):
                p = ap.get("position", {})
                szi = float(p.get("szi", 0) or 0)
                if szi:
                    positions[p["coin"]] = szi
                    details[p["coin"]] = {
                        "entry": float(p.get("entryPx") or 0),
                        "leverage": (p.get("leverage") or {}).get("value"),
                        "margin": (p.get("leverage") or {}).get("type"),
                        "value": float(p.get("positionValue") or 0),
                    }
        spot = self.info.post({"type": "spotClearinghouseState", "user": address}) or {}
        equity += sum(float(b.get("total", 0) or 0) for b in spot.get("balances", []) if b.get("coin") in STABLES)
        return LeaderBook(equity=equity, positions=positions, details=details)

    def new_tick(self) -> None:
        self._ctx.clear()

    def ctx(self, coin: str) -> AssetCtx:
        """Market data for ``coin``, one ``metaAndAssetCtxs`` call per dex per tick."""
        dex = dex_of(coin)
        if dex not in self._ctx:
            body: dict[str, Any] = {"type": "metaAndAssetCtxs"}
            if dex:
                body["dex"] = dex
            try:
                meta, ctxs = self.info.post(body)
            except requests.HTTPError:  # this network has no such dex
                meta, ctxs = {}, []
            table: dict[str, AssetCtx] = {}
            for u, c in zip(meta.get("universe", []), ctxs):
                mid = c.get("midPx") or c.get("markPx") or 0
                table[u["name"]] = AssetCtx(
                    mid=float(mid),
                    oracle=float(c.get("oraclePx") or mid or 0),
                    funding=float(c.get("funding") or 0),
                    sz_decimals=int(u.get("szDecimals", 4)),
                )
            self._ctx[dex] = table
        try:
            return self._ctx[dex][coin]
        except KeyError:
            raise KeyError(f"no market data for {coin}") from None

    def has(self, coin: str) -> bool:
        try:
            return self.ctx(coin).mid > 0
        except KeyError:
            return False


# --------------------------------------------------------------------- testnet


@dataclass
class Execution:
    filled: float  # signed size actually filled (0 if nothing)
    avg_px: float
    error: str = ""


class TestnetExecutor:
    """Real IOC orders on Hyperliquid **testnet**, via the official SDK.

    Refuses to exist for mainnet: ``settings.testnet`` must be true. Orders are
    aggressive limit IOCs capped at ``COPY_MAX_SLIPPAGE_BPS`` from the testnet
    mid; reductions and closes are ``reduce_only`` so a stale view can never
    open a position the wrong way.
    """

    def __init__(self, settings: Settings, dexes: list[str]) -> None:
        if not settings.testnet:
            raise RuntimeError("TestnetExecutor needs HL_TESTNET=true: copy trading never sends mainnet orders.")
        if not settings.private_key or not settings.wallet_address:
            raise RuntimeError("Testnet copy trading needs HL_WALLET_ADDRESS and HL_PRIVATE_KEY (testnet API wallet).")
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils.constants import TESTNET_API_URL

        wallet = Account.from_key(settings.private_key)
        address = settings.wallet_address.strip()
        self.address = address.lower()
        self.slippage = settings.copy_max_slippage_bps / 10_000
        self.exchange = Exchange(
            wallet,
            base_url=TESTNET_API_URL,
            account_address=address if address.lower() != wallet.address.lower() else None,
            perp_dexs=dexes,
            timeout=15.0,
        )

    def place(self, coin: str, delta: float, reduce_only: bool, mid: float) -> Execution:
        is_buy = delta > 0
        try:
            px = self.exchange._slippage_price(coin, is_buy, self.slippage, px=mid)  # tick-rounded
            resp = self.exchange.order(
                coin, is_buy, abs(delta), px, order_type={"limit": {"tif": "Ioc"}}, reduce_only=reduce_only
            )
        except Exception as exc:  # noqa: BLE001 - one failed order must not stop the loop
            return Execution(0.0, 0.0, f"{type(exc).__name__}: {exc}")
        return parse_order_response(resp, is_buy)


def parse_order_response(resp: Any, is_buy: bool) -> Execution:
    """Read the SDK's order response into an ``Execution``."""
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        return Execution(0.0, 0.0, str(resp.get("response") if isinstance(resp, dict) else resp)[:200])
    statuses = (((resp.get("response") or {}).get("data") or {}).get("statuses")) or []
    st = statuses[0] if statuses else {}
    if "filled" in st:
        f = st["filled"]
        size = float(f.get("totalSz", 0) or 0)
        return Execution(size if is_buy else -size, float(f.get("avgPx", 0) or 0))
    if "error" in st:
        return Execution(0.0, 0.0, str(st["error"])[:200])
    return Execution(0.0, 0.0, f"not filled: {st}"[:200])


# --------------------------------------------------------------------- planning


@dataclass
class Order:
    coin: str
    delta: float  # signed size to trade
    target: float
    reason: str  # open | add | reduce | close | flip | orphan


@dataclass
class Skip:
    coin: str
    reason: str


def plan_orders(
    *,
    leader: dict[str, float],
    prev_leader: dict[str, float],
    first_tick: bool,
    mine: dict[str, float],
    waiting: set[str],
    ratio: float,
    price: Callable[[str], float],
    decimals: Callable[[str], int],
    min_notional: float,
    allow_increase: bool,
    max_notional: float,
    sync_on_start: bool,
) -> tuple[list[Order], list[Skip], set[str]]:
    """Decide what to trade this tick. Pure: no I/O, no state mutation.

    Returns ``(orders, skips, waiting)`` where ``waiting`` is the updated set of
    coins we are deliberately not following until the leader is next flat in
    them (only used with ``sync_on_start=False``).
    """
    orders: list[Order] = []
    skips: list[Skip] = []
    waiting = set(waiting)

    if first_tick and not sync_on_start:
        waiting |= {c for c, s in leader.items() if s}

    gross = sum(abs(s) * price(c) for c, s in mine.items() if s)

    for coin in sorted(set(leader) | set(prev_leader) | set(mine)):
        lsize = leader.get(coin, 0.0)
        held = mine.get(coin, 0.0)

        if coin in waiting:
            if _sign(lsize) == 0:
                waiting.discard(coin)  # leader went flat: follow from the next entry
            if _sign(held) == 0:
                continue

        if _sign(lsize) == 0:
            if _sign(held) != 0:
                reason = "close" if _sign(prev_leader.get(coin, 0.0)) != 0 else "orphan"
                orders.append(Order(coin, -held, 0.0, reason))
            continue

        changed = first_tick or prev_leader.get(coin) != lsize
        if not changed:
            continue

        px = price(coin)
        target = round_size(lsize * ratio, decimals(coin))
        flip = _sign(held) != 0 and _sign(target) != 0 and _sign(target) != _sign(held)
        delta = round_size(target - held, decimals(coin))
        if abs(delta) <= EPS:
            continue

        increase = abs(target) > abs(held) or flip
        if increase:
            if not allow_increase:
                skips.append(Skip(coin, "paused or risk-halted: increase not followed"))
                if flip:  # still close the old side: never hold against the leader
                    orders.append(Order(coin, -held, 0.0, "close"))
                continue
            # Gross notional excluding this coin, then what this coin may add on top.
            others = gross - abs(held) * px
            if others + abs(target) * px > max_notional:
                allowed = max(max_notional - others, 0.0) / px
                if not flip:
                    allowed = max(allowed, abs(held))  # a cap never forces a reduction
                factor = 10 ** decimals(coin)
                target = math.copysign(math.floor(allowed * factor) / factor, target)
                delta = round_size(target - held, decimals(coin))
                skips.append(Skip(coin, f"capped by MAX_LEVERAGE to {target:g}"))
                if abs(delta) <= EPS:
                    continue

        if _sign(target) != 0 and abs(delta) * px < min_notional:
            skips.append(Skip(coin, f"order ${abs(delta) * px:.2f} below ${min_notional:.0f} minimum"))
            continue
        if _sign(target) != 0 and abs(target) * px < min_notional:
            # Reducing to a stub the exchange wouldn't let us hold: close instead.
            target, delta = 0.0, -held
            if abs(delta) <= EPS:
                skips.append(Skip(coin, f"position ${abs(lsize * ratio) * px:.2f} below minimum"))
                continue

        if _sign(target) == 0:
            reason = "close"
        elif flip:
            reason = "flip"
        elif _sign(held) == 0:
            reason = "open"
        else:
            reason = "add" if abs(target) > abs(held) else "reduce"
        orders.append(Order(coin, delta, target, reason))
        gross += (abs(target) - abs(held)) * px

    return orders, skips, waiting


# --------------------------------------------------------------------- ledger


@dataclass
class CopyPosition:
    coin: str
    size: float  # signed
    entry: float  # average entry of the current position
    opened_at: str
    fees: float = 0.0
    funding: float = 0.0  # received (+) / paid (-)
    realized: float = 0.0  # price P&L from reductions
    fills: int = 0
    max_abs: float = 0.0
    exit_qty: float = 0.0
    exit_notional: float = 0.0
    side: str = "buy"  # side the position was opened on

    def unrealized(self, mark: float) -> float:
        return (mark - self.entry) * self.size


class CopyPaperBroker:
    """Paper ledger for copy trading: partial fills, flips, funding, persistence.

    Exposes ``equity``, ``positions`` and ``log_event`` like the other brokers
    so the bot's heartbeat, pause flag and agent read layer work unchanged.
    ``equity`` is marked to market (cash + unrealized) whenever ``mark`` runs.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logs_dir = Path(settings.logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.cash: float = settings.initial_equity
        self.equity: float = settings.initial_equity
        self.positions: dict[str, CopyPosition] = {}
        self.marks: dict[str, float] = {}

    # -- logging (same files and shapes as _BrokerBase) --
    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _write_jsonl(self, name: str, payload: dict) -> None:
        with (self.logs_dir / name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")

    def log_event(self, event: str, payload: dict) -> None:
        record = {"ts": self._now(), "event": event}
        record.update(log_hygiene.compact_event_payload(payload))
        self._write_jsonl("events.jsonl", record)

    # -- trading --
    def fill(
        self, coin: str, delta: float, px: float, fee_bps: float, reason: str, fee: Optional[float] = None
    ) -> list[TradeResult]:
        """Apply one signed fill. Returns the trades it closed (two on a flip: none open).

        ``fee`` (absolute, in USD) overrides ``fee_bps`` -- the mirror passes the
        leader's actual fee, scaled.
        """
        closed: list[TradeResult] = []
        if fee is None:
            fee = abs(delta) * px * fee_bps / 10_000
        self.cash -= fee
        pos = self.positions.get(coin)
        remaining = delta

        if pos and _sign(pos.size) != _sign(delta):
            close_qty = min(abs(delta), abs(pos.size))
            share = close_qty / abs(delta)
            pnl = (px - pos.entry) * close_qty * _sign(pos.size)
            pos.realized += pnl
            pos.fees += fee * share
            pos.exit_qty += close_qty
            pos.exit_notional += close_qty * px
            pos.fills += 1
            self.cash += pnl
            pos.size += math.copysign(close_qty, delta)
            remaining = delta - math.copysign(close_qty, delta)
            fee *= 1 - share
            if abs(pos.size) <= EPS:
                closed.append(self._close(pos, px, reason))
                pos = None

        if abs(remaining) > EPS:
            if pos is None:
                pos = CopyPosition(
                    coin=coin,
                    size=0.0,
                    entry=px,
                    opened_at=self._now(),
                    side="buy" if remaining > 0 else "sell",
                )
                self.positions[coin] = pos
            new_size = pos.size + remaining
            pos.entry = (pos.entry * abs(pos.size) + px * abs(remaining)) / abs(new_size)
            pos.size = new_size
            pos.fees += fee
            pos.fills += 1
            pos.max_abs = max(pos.max_abs, abs(new_size))
        self.marks[coin] = px
        return closed

    def _close(self, pos: CopyPosition, px: float, reason: str) -> TradeResult:
        del self.positions[pos.coin]
        now = datetime.now(timezone.utc)
        trade = TradeResult(
            symbol=pos.coin,
            side=pos.side,  # type: ignore[arg-type]
            size=pos.max_abs,
            entry_price=pos.entry,
            exit_price=pos.exit_notional / pos.exit_qty if pos.exit_qty else px,
            pnl=pos.realized - pos.fees + pos.funding,
            fees=pos.fees,
            opened_at=datetime.fromisoformat(pos.opened_at),
            closed_at=now,
            exit_reason=f"copy_{reason}",
            legs=pos.fills,
        )
        payload = asdict(trade)
        payload["opened_at"] = trade.opened_at.isoformat()
        payload["closed_at"] = trade.closed_at.isoformat()
        payload["funding"] = pos.funding
        payload["equity"] = self.cash
        self._write_jsonl("trades.jsonl", payload)
        return trade

    def add_funding(self, coin: str, amount: float) -> None:
        """Book a funding payment in USD (+ received, - paid)."""
        self.cash += amount
        pos = self.positions.get(coin)
        if pos:
            pos.funding += amount

    def apply_funding(self, coin: str, rate: float, oracle: float) -> float:
        """Hourly funding: longs pay a positive rate, shorts receive it."""
        pos = self.positions.get(coin)
        if not pos:
            return 0.0
        payment = -pos.size * oracle * rate
        pos.funding += payment
        self.cash += payment
        return payment

    def force_size(self, coin: str, size: float, px: float) -> None:
        """Make the ledger agree with the exchange without booking P&L.

        Live mode only: the exchange's positions are the truth. A mismatch means
        an order filled differently than reported (or the account was touched by
        hand); the ledger is corrected and the event logged, not hidden.
        """
        pos = self.positions.get(coin)
        if abs(size) <= EPS:
            self.positions.pop(coin, None)
            return
        if pos is None or _sign(pos.size) != _sign(size):
            self.positions[coin] = CopyPosition(
                coin=coin, size=size, entry=px, opened_at=self._now(), side="buy" if size > 0 else "sell",
                max_abs=abs(size),
            )
        else:
            pos.size = size
            pos.max_abs = max(pos.max_abs, abs(size))

    def mark(self, marks: dict[str, float]) -> float:
        self.marks.update(marks)
        self.equity = self.cash + sum(
            p.unrealized(self.marks.get(c, p.entry)) for c, p in self.positions.items()
        )
        return self.equity

    def snapshot(self) -> list[dict[str, Any]]:
        out = []
        for coin, p in sorted(self.positions.items()):
            mark = self.marks.get(coin, p.entry)
            out.append(
                {
                    "symbol": coin,
                    "side": "buy" if p.size > 0 else "sell",
                    "size": round(abs(p.size), 8),
                    "avg_entry": round(p.entry, 6),
                    "mark": round(mark, 6),
                    "unrealized_pnl": round(p.unrealized(mark), 4),
                    "funding": round(p.funding, 4),
                    "stop_price": 0.0,
                    "peak_price": 0.0,
                    "opened_at": p.opened_at,
                }
            )
        return out

    # -- persistence --
    def to_state(self) -> dict[str, Any]:
        return {"cash": self.cash, "positions": {c: asdict(p) for c, p in self.positions.items()}}

    def load_state(self, state: dict[str, Any]) -> None:
        self.cash = float(state.get("cash", self.cash))
        self.positions = {c: CopyPosition(**p) for c, p in state.get("positions", {}).items()}
        self.equity = self.cash


# --------------------------------------------------------------------- trader


class CopyTrader:
    """Glue: poll the leader, plan, execute (paper or testnet), accrue funding, persist.

    ``client`` reads the leader (always mainnet). ``venue`` supplies execution
    prices and, in testnet mode, our own account; in paper mode it is the same
    client. With an ``executor`` the exchange's positions are the truth each
    tick and the ledger is reconciled to them; without one the ledger is.
    """

    def __init__(
        self,
        settings: Settings,
        broker: CopyPaperBroker,
        client: Optional[CopyClient] = None,
        on_trade_close: Optional[Callable[[TradeResult], None]] = None,
        can_increase: Optional[Callable[[], bool]] = None,
        venue: Optional[CopyClient] = None,
        executor: Optional[TestnetExecutor] = None,
    ) -> None:
        if not settings.copy_leader:
            raise ValueError("STRATEGY=copy needs COPY_LEADER=0x... in .env")
        self.settings = settings
        self.broker = broker
        self.client = client or CopyClient(settings)
        self.venue = venue or self.client
        self.executor = executor
        self.venue_name = "testnet" if executor else "paper"
        self.leader_address = settings.copy_leader.lower()
        self.on_trade_close = on_trade_close or (lambda t: None)
        self.can_increase = can_increase or (lambda: True)
        self.prev_leader: dict[str, float] = {}
        self.waiting: set[str] = set()
        self.first_tick = True
        self.funding_hour: Optional[int] = None
        self.leader_equity = 0.0
        self.ratio = 0.0
        self._unlisted: set[str] = set()
        self._state_path = Path(settings.logs_dir) / STATE_FILE
        self._load()

    # -- persistence --
    def _load(self) -> None:
        if not self._state_path.exists():
            return
        state = json.loads(self._state_path.read_text(encoding="utf-8"))
        if state.get("leader") != self.leader_address or state.get("venue", "paper") != self.venue_name:
            # A different leader or venue: the old book means nothing. Start clean.
            self._state_path.rename(self._state_path.with_suffix(f".{int(time.time())}.bak"))
            return
        self.broker.load_state(state.get("broker", {}))
        self.prev_leader = {c: float(s) for c, s in state.get("prev_leader", {}).items()}
        self.waiting = set(state.get("waiting", []))
        self.funding_hour = state.get("funding_hour")
        self.first_tick = False

    def _save(self) -> None:
        state = {
            "leader": self.leader_address,
            "venue": self.venue_name,
            "broker": self.broker.to_state(),
            "prev_leader": self.prev_leader,
            "waiting": sorted(self.waiting),
            "funding_hour": self.funding_hour,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        tmp.replace(self._state_path)

    def _fee_bps(self, coin: str) -> float:
        return self.settings.copy_hip3_fee_bps if dex_of(coin) else self.settings.copy_taker_fee_bps

    def _funding(self) -> list[str]:
        """Charge one hour of funding per elapsed hour boundary (at most one per tick).

        On testnet the exchange charges the real funding; this keeps the
        ledger's per-trade P&L close to it.
        """
        hour = int(time.time() // 3600)
        if self.funding_hour is None:
            self.funding_hour = hour
            return []
        if hour == self.funding_hour:
            return []
        self.funding_hour = hour
        notes = []
        for coin in list(self.broker.positions):
            c = self.venue.ctx(coin)
            paid = self.broker.apply_funding(coin, c.funding, c.oracle)
            notes.append(f"{coin} funding {paid:+.4f}")
        if notes:
            self.broker.log_event("copy_funding", {"payments": notes})
        return notes

    def _account(self) -> tuple[dict[str, float], float]:
        """Our positions and equity: the exchange's in testnet mode, the ledger's on paper."""
        if self.executor is None:
            marks = {c: self.venue.ctx(c).mid for c in self.broker.positions}
            equity = self.broker.mark(marks)
            return {c: p.size for c, p in self.broker.positions.items()}, equity
        acct = self.venue.leader(self.executor.address)
        self._reconcile(acct.positions)
        self.broker.mark({c: self.venue.ctx(c).mid for c in self.broker.positions})
        self.broker.equity = acct.equity  # the exchange's number, not the ledger's
        return acct.positions, acct.equity

    def _reconcile(self, actual: dict[str, float]) -> None:
        for coin in sorted(set(actual) | set(self.broker.positions)):
            want = actual.get(coin, 0.0)
            pos = self.broker.positions.get(coin)
            have = pos.size if pos else 0.0
            if abs(want - have) > 1e-9 * max(1.0, abs(want)):
                px = self.venue.ctx(coin).mid if self.venue.has(coin) else (pos.entry if pos else 0.0)
                self.broker.force_size(coin, want, px)
                self.broker.log_event("copy_reconcile", {"symbol": coin, "ledger": have, "exchange": want})

    def _execute(self, o: Order, held: float, mid: float) -> tuple[float, float, str]:
        """Returns ``(filled, price, error)``."""
        if self.executor is None:
            slip = self.settings.slippage_bps / 10_000
            return o.delta, (mid * (1 + slip) if o.delta > 0 else mid * (1 - slip)), ""
        reduce_only = _sign(o.target) == 0 or (
            _sign(o.target) == _sign(held) and abs(o.target) < abs(held)
        )
        ex = self.executor.place(o.coin, o.delta, reduce_only, mid)
        return ex.filled, ex.avg_px, ex.error

    def tick(self, paused: bool) -> list[str]:
        self.client.new_tick()
        if self.venue is not self.client:
            self.venue.new_tick()
        book = self.client.leader(self.leader_address)
        self.leader_equity = book.equity
        mine, equity = self._account()  # before sizing: the ratio uses current equity
        self.ratio = self.settings.copy_scale * equity / book.equity if book.equity > 0 else 0.0

        statuses: list[str] = []
        listed = {c: s for c, s in book.positions.items() if self.venue.has(c)}
        for coin in sorted(set(book.positions) - set(listed)):
            if coin not in self._unlisted:  # say it once, not every poll
                self.broker.log_event("copy_skip", {"symbol": coin, "reason": f"not listed on {self.venue_name}"})
            statuses.append(f"{coin} skipped: not listed on {self.venue_name}")
        self._unlisted = set(book.positions) - set(listed)

        orders, skips, self.waiting = plan_orders(
            leader=listed,
            prev_leader=self.prev_leader,
            first_tick=self.first_tick,
            mine=mine,
            waiting=self.waiting,
            ratio=self.ratio,
            price=lambda c: self.venue.ctx(c).mid,
            decimals=lambda c: self.venue.ctx(c).sz_decimals,
            min_notional=self.settings.copy_min_notional,
            allow_increase=not paused and self.can_increase(),
            max_notional=self.settings.max_leverage * equity,
            sync_on_start=self.settings.copy_sync_on_start,
        )

        for o in orders:
            mid = self.venue.ctx(o.coin).mid
            filled, px, error = self._execute(o, mine.get(o.coin, 0.0), mid)
            leader_size = book.positions.get(o.coin, 0.0)
            if error or abs(filled) <= EPS:
                reason = error or "nothing filled"
                self.broker.log_event(
                    "copy_order_error",
                    {"symbol": o.coin, "reason": o.reason, "size": o.delta, "error": reason},
                )
                line = f"{o.coin} {o.reason} {o.delta:+g} FAILED: {reason}"
                print(f"[{datetime.now(timezone.utc).isoformat()}] COPY {line}")
                statuses.append(line)
                continue
            closed = self.broker.fill(o.coin, filled, px, self._fee_bps(o.coin), o.reason)
            self.broker.log_event(
                "copy_fill",
                {
                    "symbol": o.coin,
                    "venue": self.venue_name,
                    "reason": o.reason,
                    "side": "buy" if filled > 0 else "sell",
                    "size": abs(filled),
                    "requested": abs(o.delta),
                    "price": px,
                    "mid": mid,
                    "target": o.target,
                    "leader_size": leader_size,
                    "ratio": self.ratio,
                    "equity": self.broker.cash,
                },
            )
            partial = "" if abs(filled - o.delta) <= EPS else f" (partial: asked {o.delta:+g})"
            line = f"{o.coin} {o.reason} {filled:+g} @ {px:g}{partial} (leader {leader_size:+g})"
            print(f"[{datetime.now(timezone.utc).isoformat()}] COPY {line}")
            statuses.append(line)
            for t in closed:
                self.on_trade_close(t)
        for s in skips:
            self.broker.log_event("copy_skip", {"symbol": s.coin, "reason": s.reason})
            statuses.append(f"{s.coin} skipped: {s.reason}")

        statuses += self._funding()
        self.prev_leader = dict(listed)
        self.first_tick = False
        if self.executor is not None and orders:
            mine, _ = self._account()  # re-read the exchange after trading
        else:
            mine = {c: p.size for c, p in self.broker.positions.items()} if self.executor is None else mine
            if self.executor is None:
                self.broker.mark({c: self.venue.ctx(c).mid for c in self.broker.positions})
        self._save()

        for coin, size in sorted(listed.items()):
            note = " (waiting for leader to go flat)" if coin in self.waiting else ""
            statuses.append(f"{coin} leader {size:+g} me {mine.get(coin, 0.0):+g}{note}")
        if not book.positions:
            statuses.append("leader flat")
        return statuses

    def summary(self) -> dict[str, Any]:
        return {
            "leader": self.leader_address,
            "venue": self.venue_name,
            "leader_equity": round(self.leader_equity, 2),
            "ratio": self.ratio,
            "waiting": sorted(self.waiting),
        }


def build_testnet(settings: Settings) -> tuple[CopyClient, TestnetExecutor]:
    """Venue client and executor for testnet copy trading.

    Only the leader's dexes that also exist on testnet are loaded: testnet lists
    hundreds of throwaway dexes, and the SDK fetches metadata for each one given.
    """
    leader = CopyClient(settings)
    testnet = CopyClient(settings, url=TESTNET_INFO_URL)
    on_testnet = set(testnet.all_dexes())
    dexes = [d for d in leader.dexes() if d in on_testnet]
    venue = CopyClient(settings, info=testnet.info, dexes=dexes)
    return venue, TestnetExecutor(settings, dexes)


def print_banner(settings: Settings) -> Iterable[str]:
    if settings.copy_mode == "mirror":
        yield (
            f"  mirror: leader={settings.copy_leader} base=${settings.initial_equity:g} x{settings.copy_scale} "
            f"poll={settings.copy_poll_seconds}s -- every leader fill replayed at its own price, no minimum"
        )
        return
    yield f"  copy: leader={settings.copy_leader} scale={settings.copy_scale} poll={settings.copy_poll_seconds}s"
    yield (
        f"        dexes={','.join(settings.copy_dexes)} sync_on_start={settings.copy_sync_on_start} "
        f"min_notional=${settings.copy_min_notional:g} risk_guards={settings.copy_risk_guards}"
    )
