"""Trend-following strategy (STRATEGY=trend), direction-aware.

A "mature" template that addresses the structural flaws of the DCA dip-buyer:

* Trade WITH the trend  - enter only when fast EMA > slow EMA AND price is on the
                          right side of the long-term (regime) EMA. Stay flat
                          otherwise instead of catching falling knives.
* Asymmetric R:R        - a single ATR-based initial stop, then a wider ATR
                          "chandelier" trailing stop so winners are allowed to
                          run while losers are cut at ~1 risk unit.
* Risk-based sizing      - size each trade so the stop distance equals a fixed
                          fraction (``risk_per_trade_pct``) of equity, capped by
                          ``max_leverage``. No fixed notional, no DCA averaging.

One position per symbol, one leg. Long is the default; symbols in
``SHORT_SYMBOLS`` use the mirror (short above-trend rallies, trail down).
"""
from __future__ import annotations

from typing import List

from .config import Settings
from .strategy import compute_atr, compute_ema

LONG = "long"
SHORT = "short"


def _closes(closed_ohlcv: List[List[float]]) -> List[float]:
    return [float(c[4]) for c in closed_ohlcv]


def atr_value(closed_ohlcv: List[List[float]], settings: Settings) -> float:
    """ATR over the configured period (0.0 if not enough data)."""
    return compute_atr(closed_ohlcv, settings.atr_period)


def trend_signal(closed_ohlcv: List[List[float]], settings: Settings, direction: str = LONG) -> bool:
    """Entry signal: EMA cross aligned with the regime EMA.

    Long:  fast > slow AND last close > regime EMA.
    Short: fast < slow AND last close < regime EMA.
    Returns False until there is enough data for the slowest EMA.
    """
    closes = _closes(closed_ohlcv)
    need = max(settings.slow_ema, settings.trend_ema_period)
    if len(closes) < need:
        return False
    fast = compute_ema(closes, settings.fast_ema)
    slow = compute_ema(closes, settings.slow_ema)
    regime = compute_ema(closes, settings.trend_ema_period)
    last = closes[-1]
    if direction == SHORT:
        return fast < slow and last < regime
    return fast > slow and last > regime


def regime_intact(closed_ohlcv: List[List[float]], settings: Settings, direction: str = LONG) -> bool:
    """True while the trend that justified the trade still holds (EMA cross).

    Used as a soft exit: when the fast/slow cross flips against the position we
    close at market rather than waiting for the trailing stop.
    """
    closes = _closes(closed_ohlcv)
    if len(closes) < settings.slow_ema:
        return True  # not enough data to declare the trend dead
    fast = compute_ema(closes, settings.fast_ema)
    slow = compute_ema(closes, settings.slow_ema)
    return fast < slow if direction == SHORT else fast > slow


def initial_stop(entry: float, atr: float, settings: Settings, direction: str = LONG) -> float:
    """Initial protective stop at ``stop_atr_multiplier`` ATRs from entry."""
    dist = settings.stop_atr_multiplier * atr
    if dist <= 0:
        return 0.0
    return entry + dist if direction == SHORT else entry - dist


def chandelier_stop(extreme: float, atr: float, settings: Settings, direction: str = LONG) -> float:
    """Trailing stop ``trail_atr_multiplier`` ATRs from the favorable extreme
    (highest high for long, lowest low for short)."""
    dist = settings.trail_atr_multiplier * atr
    if dist <= 0 or extreme <= 0:
        return 0.0
    return extreme + dist if direction == SHORT else extreme - dist


def position_size(equity: float, entry: float, stop_price: float, settings: Settings) -> float:
    """Fixed-fractional size so (entry - stop) risk equals ``risk_per_trade_pct``
    of equity, capped at ``max_leverage`` notional."""
    if entry <= 0 or stop_price <= 0:
        return 0.0
    risk_amt = equity * (settings.risk_per_trade_pct / 100.0)
    dist = abs(entry - stop_price)
    if dist <= 0:
        return 0.0
    size = risk_amt / dist
    max_notional = equity * settings.max_leverage
    if size * entry > max_notional:
        size = max_notional / entry
    return max(size, 0.0)
