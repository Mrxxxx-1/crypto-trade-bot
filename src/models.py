"""Core data types shared across bot, backtest, and exchange modules.

Signal / Side are string literals; Position, PendingOrder, Leg, and TradeResult
are plain dataclasses.  OHLCV candles are ``[ts_ms, open, high, low, close, volume]``.

Under the DCA-on-dips strategy, a ``Position`` is a composite of one or more
``Leg`` fills sharing a side and symbol.  ``entry_price`` and ``size`` are
recomputed (size-weighted average / sum) each time a leg is added or removed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Literal, Optional

Signal = Literal["long", "short", "flat"]
Side = Literal["buy", "sell"]


@dataclass
class Leg:
    """One fill within a composite (DCA) position."""

    size: float
    entry_price: float
    opened_at: datetime
    fee: float = 0.0


@dataclass
class Position:
    """An open position held by the paper broker or backtest engine.

    Fields ``size`` and ``entry_price`` are *derived* from ``legs`` (sum of
    sizes; size-weighted average of entry prices) and kept in sync by the
    broker whenever a leg is added.

    Trailing-stop fields (``trail_armed``, ``peak_price``, ``stop_price``)
    are managed by the bot/backtest loop:

    - ``trail_armed`` flips True once ``last_close >= entry_price * (1 + trail_activate_pct/100)``.
    - While armed, ``peak_price`` ratchets to the running high and
      ``stop_price = peak_price * (1 - trail_distance_pct/100)``.
    - Before armed, ``stop_price`` stays 0.0 (no hard SL in this strategy).

    ``last_fill_price`` is the entry price of the most recent leg and is the
    reference for the next DCA add (``last_close <= last_fill_price * (1 - dca_trigger_pct/100)``).
    """

    symbol: str
    side: Side
    size: float
    entry_price: float
    opened_at: datetime
    stop_price: float = 0.0
    take_profit_price: float = 0.0
    entry_candle_ts: Optional[datetime] = None
    legs: List[Leg] = field(default_factory=list)
    last_fill_price: float = 0.0
    trail_armed: bool = False
    peak_price: float = 0.0

    # Legacy fields kept for backward compat with old broker/log code paths.
    # They are no longer driven by the DCA strategy.
    initial_stop_distance: float = 0.0
    trail_atr: float = 0.0


@dataclass
class TradeResult:
    """A closed (fully-exited) position. Under DCA this aggregates all legs.

    ``entry_price`` is the size-weighted average across legs at close time;
    ``size`` is the total filled size; ``fees`` is entry+exit fees summed.
    """

    symbol: str
    side: Side
    size: float
    entry_price: float
    exit_price: float
    pnl: float
    fees: float
    opened_at: datetime
    closed_at: datetime
    exit_reason: str = ""
    legs: int = 1  # number of legs that made up the closed position


@dataclass
class PendingOrder:
    """A limit entry order waiting to fill; ``placed_at`` is used for timeout.

    Retained from the previous strategy; the DCA loop bypasses the limit
    flow and fills directly via ``open_leg``.  Still used by the legacy
    ``place_limit_entry`` codepath if anything else calls it.
    """

    symbol: str
    side: Side
    size: float
    limit_price: float
    stop_price: float
    take_profit_price: float
    placed_at: datetime
    initial_stop_distance: float = 0.0
    trail_atr: float = 0.0
    exchange_order_id: Optional[str] = None
