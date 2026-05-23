"""Long-only DCA-on-dips strategy.

Lifecycle, per symbol:
  1. **Initial leg**: open a long when ``last_close <= local_high * (1 - initial_dip_pct/100)``
     AND ``last_close <= settings.max_price_for(symbol)``.
  2. **DCA adds**: while position is open and legs < ``max_dca_legs``, open another leg
     when ``last_close <= last_fill_price * (1 - dca_trigger_pct/100)`` (cap filter still applies).
  3. **Arm trailing**: once ``last_close >= avg_entry_price * (1 + trail_activate_pct/100)``.
  4. **Exit (all legs)**: once armed, ratchet ``peak_price`` to the running high and
     exit when ``last_close <= peak_price * (1 - trail_distance_pct/100)``.

No hard SL, no fixed TP. Shorts are disabled.
``compute_atr`` is retained for diagnostic / logging callers; it no longer drives
sizing or stops.
"""
from __future__ import annotations

from typing import List

from .config import Settings


def compute_atr(ohlcv: List[List[float]], period: int) -> float:
    """Return the Average True Range over the last *period* candles.

    Kept for backward compatibility and diagnostics; not used to size positions
    or place stops under the DCA strategy.
    """
    if len(ohlcv) < period + 1:
        return 0.0
    trs: List[float] = []
    for i in range(1, len(ohlcv)):
        high = float(ohlcv[i][2])
        low = float(ohlcv[i][3])
        prev_close = float(ohlcv[i - 1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    recent = trs[-period:]
    return sum(recent) / len(recent)


def local_high(ohlcv: List[List[float]], lookback: int) -> float:
    """Return the maximum high price over the last ``lookback`` candles.

    Falls back to the max over whatever data is available if the series is
    shorter than ``lookback``.  Returns 0.0 only if the series is empty.
    """
    if not ohlcv:
        return 0.0
    window = ohlcv[-lookback:] if lookback > 0 else ohlcv
    return max(float(c[2]) for c in window)


def entry_trigger(ohlcv: List[List[float]], settings: Settings) -> bool:
    """True if a qualifying dip happened recently AND (optionally) the latest bar confirms.

    Logic:
      1. Compute ``threshold = local_high(lookback) * (1 - initial_dip_pct/100)``.
      2. ``recent_dip``: any of the last ``dip_memory_bars`` closed bars closed at or below ``threshold``.
         This gives the entry a short memory window so we don't miss it on a fast bounce.
      3. ``green_ok``: if ``require_green_confirmation`` is True, the most recent closed bar
         must be bullish (close > open).  Filters out catching a still-falling knife.

    Returns True only when both conditions pass.  Requires at least 2 candles.
    """
    if len(ohlcv) < 2:
        return False
    high = local_high(ohlcv, settings.high_lookback_candles)
    if high <= 0:
        return False
    threshold = high * (1.0 - settings.initial_dip_pct / 100.0)

    memory = max(1, settings.dip_memory_bars)
    recent = ohlcv[-memory:]
    recent_dip = any(float(c[4]) <= threshold for c in recent)
    if not recent_dip:
        return False

    if settings.require_green_confirmation:
        last_open = float(ohlcv[-1][1])
        last_close = float(ohlcv[-1][4])
        if last_close <= last_open:
            return False

    return True


def dca_trigger(last_close: float, last_fill_price: float, settings: Settings) -> bool:
    """True if price has dropped >= ``dca_trigger_pct`` below the most recent leg's fill."""
    if last_fill_price <= 0 or last_close <= 0:
        return False
    threshold = last_fill_price * (1.0 - settings.dca_trigger_pct / 100.0)
    return last_close <= threshold


def should_arm_trail(avg_cost: float, last_close: float, settings: Settings) -> bool:
    """True once net unrealized P&L reaches ``trail_activate_pct`` above avg cost."""
    if avg_cost <= 0:
        return False
    threshold = avg_cost * (1.0 + settings.trail_activate_pct / 100.0)
    return last_close >= threshold


def trail_stop_price(peak_price: float, settings: Settings) -> float:
    """Return the trailing-stop price = ``peak_price * (1 - trail_distance_pct/100)``."""
    if peak_price <= 0:
        return 0.0
    return peak_price * (1.0 - settings.trail_distance_pct / 100.0)


def price_in_zone(symbol: str, last_close: float, settings: Settings) -> bool:
    """True if ``last_close`` is at or below the symbol's long-entry ceiling.

    Uncapped symbols (not listed in ``LONG_MAX_PRICES``) pass through.
    """
    cap = settings.max_price_for(symbol)
    return last_close <= cap
