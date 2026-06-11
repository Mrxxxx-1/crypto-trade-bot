"""DCA strategy, direction-aware (long dip-buying OR short rip-selling).

Per symbol the bot trades ONE direction, chosen by ``Settings.direction_for``:

* ``long``  — buy dips below a recent high, DCA down, trail up, exit on a drop
              from the peak (the original strategy).
* ``short`` — the mirror image: sell rips above a recent low, DCA up, trail
              down, exit on a bounce from the trough.

Every helper takes ``direction`` ("long" / "short") so the bot and backtester
share one implementation.  Long is the default to preserve old call sites.

Price/▷profit relationships (``d`` = direction):
              long                         short
  entry       close <= high*(1-dip%)       close >= low*(1+dip%)
  dca add     close <= last_fill*(1-dca%)  close >= last_fill*(1+dca%)
  arm trail   close >= avg*(1+act%)        close <= avg*(1-act%)
  trail stop  peak*(1-dist%)               trough*(1+dist%)
  stop loss   avg*(1-sl%)                  avg*(1+sl%)
  take profit avg*(1+tp%)                  avg*(1-tp%)
  trend ok    close > EMA                  close < EMA
"""
from __future__ import annotations

from typing import List

from .config import Settings

LONG = "long"
SHORT = "short"


def compute_atr(ohlcv: List[List[float]], period: int) -> float:
    """Average True Range over the last *period* candles (diagnostics only)."""
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


def compute_ema(values: List[float], period: int) -> float:
    """Standard EMA over ``values``; seeds from the first value. 0.0 if empty."""
    if not values or period <= 0:
        return 0.0
    k = 2.0 / (period + 1.0)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1.0 - k)
    return ema


def in_trend(closed_ohlcv: List[List[float]], settings: Settings, direction: str = LONG) -> bool:
    """Trend filter. Long: price above the long EMA. Short: price below it.

    Disabled filter always returns True. Returns False until there is enough
    data for the EMA (stay out until the trend is known).
    """
    if not settings.trend_filter_enabled:
        return True
    period = settings.trend_ema_period
    if period <= 0:
        return True
    closes = [float(c[4]) for c in closed_ohlcv]
    if len(closes) < period:
        return False
    ema = compute_ema(closes, period)
    last = closes[-1]
    return last < ema if direction == SHORT else last > ema


def local_extreme(ohlcv: List[List[float]], lookback: int, direction: str = LONG) -> float:
    """Reference price for entries: the highest high (long) or lowest low (short)."""
    if not ohlcv:
        return 0.0
    window = ohlcv[-lookback:] if lookback > 0 else ohlcv
    if direction == SHORT:
        return min(float(c[3]) for c in window)
    return max(float(c[2]) for c in window)


# Back-compat alias used by some callers/tests.
def local_high(ohlcv: List[List[float]], lookback: int) -> float:
    return local_extreme(ohlcv, lookback, LONG)


def entry_trigger(ohlcv: List[List[float]], settings: Settings, direction: str = LONG) -> bool:
    """True when a qualifying move happened recently AND the latest bar confirms.

    Long: a dip of ``initial_dip_pct`` below the recent high, confirmed by a green
    bar. Short: a rip of ``initial_dip_pct`` above the recent low, confirmed by a
    red bar. ``require_green_confirmation`` gates the confirmation bar in both.
    """
    if len(ohlcv) < 2:
        return False
    ref = local_extreme(ohlcv, settings.high_lookback_candles, direction)
    if ref <= 0:
        return False

    memory = max(1, settings.dip_memory_bars)
    recent = ohlcv[-memory:]

    if direction == SHORT:
        threshold = ref * (1.0 + settings.initial_dip_pct / 100.0)
        if not any(float(c[4]) >= threshold for c in recent):
            return False
        if settings.require_green_confirmation:
            # confirmation = a red (bearish) bar
            if float(ohlcv[-1][4]) >= float(ohlcv[-1][1]):
                return False
        return True

    threshold = ref * (1.0 - settings.initial_dip_pct / 100.0)
    if not any(float(c[4]) <= threshold for c in recent):
        return False
    if settings.require_green_confirmation:
        if float(ohlcv[-1][4]) <= float(ohlcv[-1][1]):
            return False
    return True


def dca_trigger(last_close: float, last_fill_price: float, settings: Settings, direction: str = LONG) -> bool:
    """Long: price dropped >= ``dca_trigger_pct`` below last fill. Short: rose above it."""
    if last_fill_price <= 0 or last_close <= 0:
        return False
    if direction == SHORT:
        return last_close >= last_fill_price * (1.0 + settings.dca_trigger_pct / 100.0)
    return last_close <= last_fill_price * (1.0 - settings.dca_trigger_pct / 100.0)


def should_arm_trail(avg_cost: float, last_close: float, settings: Settings, direction: str = LONG) -> bool:
    """True once unrealized P&L reaches ``trail_activate_pct`` in the favorable direction."""
    if avg_cost <= 0:
        return False
    if direction == SHORT:
        return last_close <= avg_cost * (1.0 - settings.trail_activate_pct / 100.0)
    return last_close >= avg_cost * (1.0 + settings.trail_activate_pct / 100.0)


def trail_stop_price(extreme_price: float, settings: Settings, direction: str = LONG) -> float:
    """Trailing stop from the favorable extreme (peak for long, trough for short)."""
    if extreme_price <= 0:
        return 0.0
    if direction == SHORT:
        return extreme_price * (1.0 + settings.trail_distance_pct / 100.0)
    return extreme_price * (1.0 - settings.trail_distance_pct / 100.0)


def take_profit_price(avg_cost: float, settings: Settings, direction: str = LONG) -> float:
    """Fixed take-profit target; 0.0 when disabled. Long above avg, short below."""
    if settings.take_profit_pct <= 0 or avg_cost <= 0:
        return 0.0
    if direction == SHORT:
        return avg_cost * (1.0 - settings.take_profit_pct / 100.0)
    return avg_cost * (1.0 + settings.take_profit_pct / 100.0)


def stop_loss_price(avg_cost: float, settings: Settings, direction: str = LONG) -> float:
    """Hard catastrophe stop; 0.0 when disabled. Long below avg, short above."""
    if settings.stop_loss_pct <= 0 or avg_cost <= 0:
        return 0.0
    if direction == SHORT:
        return avg_cost * (1.0 + settings.stop_loss_pct / 100.0)
    return avg_cost * (1.0 - settings.stop_loss_pct / 100.0)


def price_in_zone(symbol: str, last_close: float, settings: Settings, direction: str = LONG) -> bool:
    """Long: at/below the per-symbol price ceiling. Short: always in zone (uncapped)."""
    if direction == SHORT:
        return True
    return last_close <= settings.max_price_for(symbol)
