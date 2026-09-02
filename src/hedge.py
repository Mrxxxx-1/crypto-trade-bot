"""Manual catalyst hedge: state machine, cross-process channel, and stop sizing.

A *hedge* is a mirrored pair of legs on one coin — long in the main account,
short in a sub-account — opened at the same moment, at the same size, with the
same stop distance. When direction emerges, the losing leg is closed for a
capped loss and the winner keeps a trailing stop.

Why this exists at all, stated plainly so the next reader does not have to
rediscover it: the loser's realized loss always equals the winner's unrealized
gain at the moment of the cut, so a hedge is *not* a way to make money. Its one
real benefit is that the surviving leg's cost basis is fixed the instant the
hedge opens, so a violent catalyst gap cannot slip your entry. It is therefore
armed **manually, before a known event**, and never on a schedule. The
``--entry-mode squeeze`` backtest in ``src/backtest_straddle.py`` measures what
happens when you automate it: profit factor drops below 1.

Lifecycle
---------
``requested`` -> ``open`` -> ``cut`` -> ``closed``   (or ``expired`` / ``failed``)

Processes involved
------------------
``src/telegram_control.py`` runs separately from the bot, so arming is a
*request* written to ``logs/hedge.json``; the bot loop picks it up, acts, and
writes back the resulting state. This mirrors ``src/control.py``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .config import Settings
from .strategy_squeeze import atr_series, percentile_rank

HEDGE_FILENAME = "hedge.json"

LONG = "long"
SHORT = "short"

# Request states
REQUESTED = "requested"
OPEN = "open"
CUT = "cut"
CLOSED = "closed"
EXPIRED = "expired"
FAILED = "failed"

TERMINAL_STATES = (CLOSED, EXPIRED, FAILED)


def hedge_path(logs_dir: str | Path) -> Path:
    return Path(logs_dir) / HEDGE_FILENAME


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Reference ATR: the fix for the squeeze finding
# ---------------------------------------------------------------------------

def reference_atr(
    closed_ohlcv: list[list[float]],
    atr_period: int,
    floor_percentile: float,
    lookback: int = 120,
) -> float:
    """ATR to size stops from, floored at a percentile of its own history.

    Hedges are armed before catalysts, which is exactly when realized volatility
    is compressed — and the straddle backtest showed that sizing stops off a
    squeezed ATR gets *both* legs whipsawed when the event finally moves price.
    Flooring the ATR at, say, its median keeps the stop wide enough to survive
    the expansion the hedge exists to capture.

    Returns the plain current ATR when ``floor_percentile <= 0`` or when there is
    not enough history to build a distribution.
    """
    series = atr_series(closed_ohlcv, atr_period)
    if not series:
        return 0.0
    current = series[-1]
    if floor_percentile <= 0:
        return current

    window = series[-lookback:]
    if len(window) < 2:
        return current

    ordered = sorted(window)
    # Nearest-rank percentile: the value that `floor_percentile`% of the sample
    # falls at or below.
    index = min(len(ordered) - 1, max(0, round(floor_percentile / 100.0 * len(ordered)) - 1))
    floor = ordered[index]
    return max(current, floor)


def atr_percentile(closed_ohlcv: list[list[float]], atr_period: int, lookback: int = 120) -> Optional[float]:
    """Where current ATR sits in its own recent distribution, for logging."""
    series = atr_series(closed_ohlcv, atr_period)
    window = series[-lookback:]
    if len(window) < 2:
        return None
    return percentile_rank(window, window[-1])


# ---------------------------------------------------------------------------
# Leg + hedge records
# ---------------------------------------------------------------------------

@dataclass
class HedgeLeg:
    """One side of the pair, in whichever account holds it."""

    direction: str
    account: str              # "main" or "sub"
    size: float = 0.0
    entry_price: float = 0.0
    stop_price: float = 0.0
    peak_price: float = 0.0
    opened_at: Optional[str] = None
    closed_at: Optional[str] = None
    exit_price: float = 0.0
    exit_reason: str = ""
    realized_pnl: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None and self.closed_at is None

    @property
    def side(self) -> str:
        """Order side that opens this leg."""
        return "buy" if self.direction == LONG else "sell"

    def unrealized(self, price: float) -> float:
        if not self.is_open or self.entry_price <= 0:
            return 0.0
        raw = (price - self.entry_price) * self.size
        return raw if self.direction == LONG else -raw


@dataclass
class HedgeState:
    """The whole hedge, serialized to logs/hedge.json."""

    symbol: str
    state: str = REQUESTED
    note: str = ""
    requested_by: str = ""
    requested_at: Optional[str] = None
    opened_at: Optional[str] = None
    closed_at: Optional[str] = None
    ref_price: float = 0.0
    ref_atr: float = 0.0
    atr_pct: Optional[float] = None
    winner: Optional[str] = None
    cut_price: float = 0.0
    cut_at: Optional[str] = None
    cut_reason: str = ""
    error: str = ""
    legs: dict[str, HedgeLeg] = field(default_factory=dict)

    # -- lifecycle helpers -------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self.state not in TERMINAL_STATES

    @property
    def open_legs(self) -> list[HedgeLeg]:
        return [leg for leg in self.legs.values() if leg.is_open]

    @property
    def realized_pnl(self) -> float:
        return sum(leg.realized_pnl for leg in self.legs.values())

    def leg(self, direction: str) -> Optional[HedgeLeg]:
        return self.legs.get(direction)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["legs"] = {k: asdict(v) for k, v in self.legs.items()}
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HedgeState":
        leg_names = {f.name for f in fields(HedgeLeg)}
        state_names = {f.name for f in fields(cls)} - {"legs"}

        raw_legs = data.get("legs") or {}
        legs: dict[str, HedgeLeg] = {}
        for direction, leg in raw_legs.items():
            if isinstance(leg, dict):
                allowed = {k: v for k, v in leg.items() if k in leg_names}
                legs[direction] = HedgeLeg(**{"direction": direction, "account": "main", **allowed})

        kwargs = {k: v for k, v in data.items() if k in state_names}
        kwargs.setdefault("symbol", "")
        return cls(**kwargs, legs=legs)

    def expired(self, expiry_hours: float, now: Optional[datetime] = None) -> bool:
        """True when a still-unopened request has gone stale."""
        if self.state != REQUESTED or expiry_hours <= 0:
            return False
        requested = _parse_iso(self.requested_at)
        if requested is None:
            return False
        return (now or _utc_now()) - requested >= timedelta(hours=expiry_hours)

    def stale(self, max_hours: float, now: Optional[datetime] = None) -> bool:
        """True when an opened hedge has sat un-triggered past its shelf life."""
        if self.state != OPEN or max_hours <= 0:
            return False
        opened = _parse_iso(self.opened_at)
        if opened is None:
            return False
        return (now or _utc_now()) - opened >= timedelta(hours=max_hours)

    def summary(self) -> str:
        """One-line human summary for Telegram."""
        bits = [f"{self.symbol} {self.state}"]
        if self.state == REQUESTED and self.note:
            bits.append(f"note={self.note}")
        if self.ref_price:
            bits.append(f"ref={self.ref_price:.4f}")
        for direction in (LONG, SHORT):
            leg = self.legs.get(direction)
            if not leg:
                continue
            if leg.is_open:
                bits.append(f"{direction}: {leg.size:.6f} @ {leg.entry_price:.4f} stop {leg.stop_price:.4f}")
            elif leg.closed_at:
                bits.append(f"{direction}: closed {leg.exit_reason} pnl {leg.realized_pnl:+.2f}")
        if self.winner:
            bits.append(f"winner={self.winner}")
        if self.error:
            bits.append(f"error={self.error}")
        return " | ".join(bits)


# ---------------------------------------------------------------------------
# Cross-process channel (mirrors src/control.py)
# ---------------------------------------------------------------------------

def read_hedge(logs_dir: str | Path) -> Optional[HedgeState]:
    """Current hedge record, or None when there has never been one."""
    path = hedge_path(logs_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not data.get("symbol"):
        return None
    return HedgeState.from_dict(data)


def write_hedge(logs_dir: str | Path, state: HedgeState) -> HedgeState:
    path = hedge_path(logs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(path)
    return state


def active_hedge(logs_dir: str | Path) -> Optional[HedgeState]:
    state = read_hedge(logs_dir)
    return state if state and state.is_active else None


def request_hedge(
    settings: Settings,
    symbol: str,
    note: str = "",
    by: str = "telegram",
) -> dict[str, Any]:
    """Ask the bot loop to open a hedge on ``symbol``.

    Validation happens here so Telegram gets an immediate, useful rejection
    instead of silence. Only *one* hedge may be active at a time: hedges are for
    single scheduled events, and concurrent pairs would double the margin drain
    on the sub-account.
    """
    if not settings.hedge_enabled:
        return {"ok": False, "error": "HEDGE_ENABLED is false"}
    if not settings.hedge_sub_account.strip():
        return {"ok": False, "error": "HEDGE_SUB_ACCOUNT is not set"}

    resolved = resolve_symbol(settings, symbol)
    if resolved is None:
        allowed = ", ".join(settings.hedge_symbols) or "(none)"
        return {"ok": False, "error": f"{symbol} not in HEDGE_SYMBOLS ({allowed})"}

    logs = settings.logs_dir
    existing = active_hedge(logs)
    if existing is not None:
        return {
            "ok": False,
            "error": f"hedge already {existing.state} on {existing.symbol}; /hedge close it first",
        }

    state = HedgeState(
        symbol=resolved,
        state=REQUESTED,
        note=note.strip(),
        requested_by=by,
        requested_at=_iso(_utc_now()),
    )
    write_hedge(logs, state)
    return {"ok": True, "action": "arm", "symbol": resolved, "state": REQUESTED, "note": state.note}


def request_close(settings: Settings, by: str = "telegram") -> dict[str, Any]:
    """Ask the bot loop to flatten whatever the hedge currently holds."""
    logs = settings.logs_dir
    state = active_hedge(logs)
    if state is None:
        return {"ok": False, "error": "no active hedge"}
    if state.state == REQUESTED:
        state.state = EXPIRED
        state.note = f"disarmed by {by}"
        state.closed_at = _iso(_utc_now())
        write_hedge(logs, state)
        return {"ok": True, "action": "disarm", "symbol": state.symbol, "state": state.state}

    state.cut_reason = f"manual_close_by_{by}"
    write_hedge(logs, state)
    return {
        "ok": True,
        "action": "close_requested",
        "symbol": state.symbol,
        "state": state.state,
        "detail": "the bot loop will flatten both legs on its next poll",
    }


def hedge_status(settings: Settings) -> dict[str, Any]:
    state = read_hedge(settings.logs_dir)
    if state is None:
        return {
            "hedge_enabled": settings.hedge_enabled,
            "configured": settings.hedge_configured,
            "state": "none",
            "symbols": settings.hedge_symbols,
        }
    payload = state.to_dict()
    payload.update(
        {
            "hedge_enabled": settings.hedge_enabled,
            "configured": settings.hedge_configured,
            "active": state.is_active,
            "realized_pnl": state.realized_pnl,
            "summary": state.summary(),
        }
    )
    return payload


def resolve_symbol(settings: Settings, symbol: str) -> Optional[str]:
    """Match a loose user input like ``btc`` to a configured hedge symbol."""
    wanted = symbol.strip().upper()
    if not wanted:
        return None
    for candidate in settings.hedge_symbols:
        base = candidate.split("/")[0].strip().upper()
        if wanted in (candidate.strip().upper(), base):
            return candidate
    return None
