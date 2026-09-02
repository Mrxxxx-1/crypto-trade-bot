"""Core data types shared across bot, backtest, and exchange modules.

``Side`` is a string literal; ``Position``, ``Leg``, and ``TradeResult`` are
plain dataclasses. OHLCV candles are ``[ts_ms, open, high, low, close, volume]``.

A ``Position`` is a composite of one or more ``Leg`` fills sharing a side and
symbol, with ``entry_price``/``size`` derived from the legs. The trend strategy
only ever opens a single leg, so the list is length 1 in practice; the structure
is retained because the broker's fill accounting is built on it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Literal, Optional

Side = Literal["buy", "sell"]


@dataclass
class Leg:
    """One fill within a position."""

    size: float
    entry_price: float
    opened_at: datetime
    fee: float = 0.0


@dataclass
class Position:
    """An open position held by the paper broker or the live broker.

    ``size`` and ``entry_price`` are *derived* from ``legs`` (sum of sizes;
    size-weighted average of entry prices) and kept in sync by the broker.

    The stop fields are driven by the bot loop:

    - ``stop_price`` starts at the ATR initial stop and only ever ratchets in
      the favorable direction (the chandelier trail).
    - ``peak_price`` tracks the most favorable price seen since entry: the
      highest high for a long, the lowest low for a short. The trail is measured
      from it.
    """

    symbol: str
    side: Side
    size: float
    entry_price: float
    opened_at: datetime
    stop_price: float = 0.0
    entry_candle_ts: Optional[datetime] = None
    legs: List[Leg] = field(default_factory=list)
    last_fill_price: float = 0.0
    peak_price: float = 0.0


@dataclass
class TradeResult:
    """A closed (fully-exited) position.

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
