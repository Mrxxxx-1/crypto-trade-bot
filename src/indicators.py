"""Shared, I/O-free technical indicators and entry-quality gates.

Pure functions over OHLCV rows (``[ts_ms, open, high, low, close, volume]``), so
the live bot and the backtester compute identical values from identical inputs.

Split into two layers:

* **Raw math** -- ``compute_atr``, ``compute_ema``, ``compute_adx``. No Settings,
  no opinions, just numbers.
* **Gates** -- ``adx_ok``, ``volume_ok``, ``htf_trend_ok``. These read thresholds
  off ``Settings`` and answer "is an entry allowed?". All three are opt-in and
  return True when their knob is disabled, so a default config is unfiltered.

Gates share one convention: when there isn't enough data to decide, they return
False (stay out) rather than True. The exception is a disabled gate, which is
always True.
"""
from __future__ import annotations

from statistics import fmean
from typing import List, Optional, Sequence

from .config import Settings

LONG = "long"
SHORT = "short"


def true_ranges(ohlcv: Sequence[Sequence[float]]) -> List[float]:
    """Wilder true range for every bar after the first."""
    out: List[float] = []
    for i in range(1, len(ohlcv)):
        high = float(ohlcv[i][2])
        low = float(ohlcv[i][3])
        prev_close = float(ohlcv[i - 1][4])
        out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return out


def compute_atr(ohlcv: List[List[float]], period: int) -> float:
    """Average True Range over the last ``period`` candles (0.0 if short on data)."""
    if len(ohlcv) < period + 1:
        return 0.0
    recent = true_ranges(ohlcv)[-period:]
    return sum(recent) / len(recent)


def atr_series(ohlcv: Sequence[Sequence[float]], period: int) -> List[float]:
    """Every rolling ATR reading, not just the latest.

    Same SMA-of-true-range as ``compute_atr``, so the last element equals
    ``compute_atr(ohlcv, period)``. The hedge ranks current ATR against this
    history to avoid sizing a stop off a compressed reading.
    """
    if period < 1:
        return []
    trs = true_ranges(ohlcv)
    if len(trs) < period:
        return []
    return [fmean(trs[i - period : i]) for i in range(period, len(trs) + 1)]


def percentile_rank(history: Sequence[float], value: float) -> Optional[float]:
    """Share of ``history`` at or below ``value``, as a percentage.

    A rank of 0 means nothing in the sample was calmer; 100 means nothing was
    more volatile. None when there is no history to rank against.
    """
    if not history:
        return None
    at_or_below = sum(1 for h in history if h <= value)
    return 100.0 * at_or_below / len(history)


def compute_ema(values: List[float], period: int) -> float:
    """Standard EMA over ``values``; seeds from the first value. 0.0 if empty."""
    if not values or period <= 0:
        return 0.0
    k = 2.0 / (period + 1.0)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1.0 - k)
    return ema


def compute_adx(ohlcv: List[List[float]], period: int) -> float:
    """Wilder's Average Directional Index over the last ``period`` bars.

    Measures trend *strength*, not direction: low ADX = choppy/ranging, high ADX
    = a strong directional move. Returns 0.0 when there isn't enough data (needs
    ~2*period+1 bars for a stable value).
    """
    if period <= 0 or len(ohlcv) < 2 * period + 1:
        return 0.0

    trs: List[float] = []
    plus_dm: List[float] = []
    minus_dm: List[float] = []
    for i in range(1, len(ohlcv)):
        high = float(ohlcv[i][2])
        low = float(ohlcv[i][3])
        prev_high = float(ohlcv[i - 1][2])
        prev_low = float(ohlcv[i - 1][3])
        prev_close = float(ohlcv[i - 1][4])
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    def _wilder_smooth(values: List[float]) -> List[float]:
        """Running Wilder smoothing; returns the smoothed series."""
        if len(values) < period:
            return []
        smoothed = [sum(values[:period])]
        for v in values[period:]:
            smoothed.append(smoothed[-1] - smoothed[-1] / period + v)
        return smoothed

    tr_s = _wilder_smooth(trs)
    plus_s = _wilder_smooth(plus_dm)
    minus_s = _wilder_smooth(minus_dm)
    if not tr_s or len(plus_s) != len(tr_s) or len(minus_s) != len(tr_s):
        return 0.0

    dxs: List[float] = []
    for tr_v, p_v, m_v in zip(tr_s, plus_s, minus_s):
        if tr_v <= 0:
            continue
        plus_di = 100.0 * (p_v / tr_v)
        minus_di = 100.0 * (m_v / tr_v)
        denom = plus_di + minus_di
        if denom <= 0:
            continue
        dxs.append(100.0 * abs(plus_di - minus_di) / denom)

    if len(dxs) < period:
        return 0.0
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def adx_ok(closed_ohlcv: List[List[float]], settings: Settings) -> bool:
    """True when trend strength clears the chop filter (``ADX >= ADX_MIN``).

    Disabled (always True) when ``ADX_MIN <= 0``. Below the threshold the market
    is treated as choppy and crossover entries are suppressed.
    """
    if settings.adx_min <= 0:
        return True
    adx = compute_adx(closed_ohlcv, settings.adx_period)
    if adx <= 0:
        return False  # not enough data / undefined -> stay out
    return adx >= settings.adx_min


def volume_ok(closed_ohlcv: List[List[float]], settings: Settings) -> bool:
    """Confirm the latest closed bar traded on above-average volume.

    Rule: ``last_volume > VOLUME_MIN_MULT * SMA(volume, VOLUME_MA_PERIOD)``.
    Disabled (always True) when ``VOLUME_MIN_MULT <= 0``.
    """
    if settings.volume_min_mult <= 0:
        return True
    period = max(1, settings.volume_ma_period)
    if len(closed_ohlcv) < period + 1:
        return False
    vols = [float(c[5]) for c in closed_ohlcv]
    ma = sum(vols[-period:]) / period
    if ma <= 0:
        return False
    return vols[-1] > settings.volume_min_mult * ma


def htf_trend_ok(
    htf_closed_ohlcv: List[List[float]],
    settings: Settings,
    direction: str = LONG,
) -> bool:
    """Multi-timeframe alignment gate against the higher-timeframe EMA.

    Long entries require HTF close > HTF ``MTF_EMA_PERIOD`` EMA; short entries
    require HTF close < that EMA. Disabled (always True) when ``MTF_ENABLED`` is
    False.
    """
    if not settings.mtf_enabled:
        return True
    period = settings.mtf_ema_period
    if period <= 0:
        return True
    closes = [float(c[4]) for c in htf_closed_ohlcv]
    if len(closes) < period:
        return False
    ema = compute_ema(closes, period)
    last = closes[-1]
    return last < ema if direction == SHORT else last > ema
