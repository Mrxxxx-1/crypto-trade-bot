"""Core data types shared across bot, backtest, and exchange modules.

Signal / Side are string literals; Position, PendingOrder, and TradeResult
are plain dataclasses.  OHLCV candles are ``[ts_ms, open, high, low, close, volume]``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

Signal = Literal["long", "short", "flat"]
Side = Literal["buy", "sell"]


@dataclass
class Position:
    """An open position held by the paper broker or backtest engine.

    ``entry_candle_ts`` is the UTC timestamp of the candle during which the
    position was filled.  Exit checks are skipped on this candle to match
    the backtest's behaviour.
    """
    symbol: str
    side: Side
    size: float
    entry_price: float
    stop_price: float
    take_profit_price: float
    opened_at: datetime
    entry_candle_ts: Optional[datetime] = None


@dataclass
class TradeResult:
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


@dataclass
class PendingOrder:
    """A limit entry order waiting to fill; ``placed_at`` is used for timeout."""

    symbol: str
    side: Side
    size: float
    limit_price: float
    stop_price: float
    take_profit_price: float
    placed_at: datetime
    order_id: str = ""
