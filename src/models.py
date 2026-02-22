from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Signal = Literal["long", "short", "flat"]
Side = Literal["buy", "sell"]


@dataclass
class Position:
    symbol: str
    side: Side
    size: float
    entry_price: float
    stop_price: float
    take_profit_price: float
    opened_at: datetime


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
