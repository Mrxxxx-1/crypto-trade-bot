"""Per-bar direction resolution for the trend strategy.

``Settings.direction_for`` pins a symbol to one direction for the life of the
process: short if its base coin is in ``SHORT_SYMBOLS``, long otherwise. That
means a symbol can never take the other side, however clearly the trend calls
for it -- ``SHORT_SYMBOLS=BTC,ETH`` shorts every BTC/ETH signal and stands aside
through every rally.

This module adds an opt-in alternative selected by ``DIRECTION_MODE``:

* ``static`` (default) -- delegate to ``Settings.direction_for``. Byte-for-byte
  the historical behaviour, so existing configs and backtest figures hold.
* ``signal`` -- ask the strategy which way the trend actually points and trade
  that side. ``SHORT_SYMBOLS`` becomes irrelevant for the trend strategy.

Two invariants matter more than the mode:

1. **An open position never flips.** Direction is recovered from the position's
   own ``side`` while it is open. Re-resolving mid-trade would invert the stop
   and trail comparisons and turn a protective stop into a target.
2. **No signal means no change of stance.** When neither side signals, we fall
   back to the static direction so callers still emit a coherent
   ``no_entry(<direction>)`` diagnostic.

``Settings.direction_for`` is now consulted only through this module.
"""
from __future__ import annotations

from typing import List, Optional, Protocol

from .config import Settings
from .strategy_trend import LONG, SHORT, trend_signal

STATIC = "static"
SIGNAL = "signal"
VALID_MODES = (STATIC, SIGNAL)


class _HasSide(Protocol):
    """Anything carrying a ``"buy"``/``"sell"`` side (``Position``, ``BTPosition``)."""

    side: str


def direction_of(position: _HasSide) -> str:
    """Recover the direction an open position was entered with."""
    return SHORT if str(position.side).lower() == "sell" else LONG


def signal_direction(closed_ohlcv: List[List[float]], settings: Settings) -> Optional[str]:
    """Return the side the trend currently favours, or None when it is unclear.

    The long and short trend rules are strict mirrors (fast > slow and above the
    regime EMA versus fast < slow and below it), so at most one can hold. Both
    failing is the common case and means "stand aside".
    """
    if trend_signal(closed_ohlcv, settings, LONG):
        return LONG
    if trend_signal(closed_ohlcv, settings, SHORT):
        return SHORT
    return None


def resolve(
    symbol: str,
    settings: Settings,
    closed_ohlcv: List[List[float]],
    position: Optional[_HasSide] = None,
) -> str:
    """Direction to trade ``symbol`` on this bar.

    An open ``position`` always wins, regardless of mode. Otherwise ``signal``
    mode reads the trend and ``static`` mode uses ``SHORT_SYMBOLS``.
    """
    if position is not None:
        return direction_of(position)
    if settings.direction_mode == SIGNAL:
        return signal_direction(closed_ohlcv, settings) or settings.direction_for(symbol)
    return settings.direction_for(symbol)
