"""EMA crossover + ATR volatility filter strategy.

``generate_signal`` expects **closed candles only** (the forming candle must
be excluded by the caller).  ``htf_trend`` applies the same EMA logic on a
higher-timeframe series; ``volume_ok`` gates entries on minimum volume.
"""
from __future__ import annotations

from typing import List, Tuple

from .config import Settings
from .models import Signal


def _ema(values: List[float], period: int) -> float:
    if len(values) < period:
        return values[-1]
    multiplier = 2 / (period + 1)
    ema_value = values[0]
    for value in values[1:]:
        ema_value = (value - ema_value) * multiplier + ema_value
    return ema_value


def compute_atr(ohlcv: List[List[float]], period: int) -> float:
    """Return the Average True Range over the last *period* candles.

    Public wrapper so ``bot.py`` and ``backtest.py`` can compute ATR on
    arbitrary candle series (e.g. HTF candles for wider stop placement).
    """
    return _atr(ohlcv, period)


def _atr(ohlcv: List[List[float]], period: int) -> float:
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


def generate_signal(ohlcv: List[List[float]], settings: Settings) -> Tuple[Signal, float]:
    """Return ``(signal, atr)`` from closed candle data.

    Long when fast EMA > slow EMA, short when fast < slow.
    Returns ``("flat", atr)`` when ATR < ``atr_min_pct`` or data is too short.
    """
    closes = [float(c[4]) for c in ohlcv]
    if len(closes) < max(settings.fast_ema, settings.slow_ema) + 2:
        return "flat", 0.0

    fast = _ema(closes[-settings.fast_ema :], settings.fast_ema)
    slow = _ema(closes[-settings.slow_ema :], settings.slow_ema)
    atr_value = _atr(ohlcv, settings.atr_period)
    last_close = closes[-1]

    if last_close <= 0:
        return "flat", atr_value

    atr_pct = (atr_value / last_close) * 100
    if atr_pct < settings.atr_min_pct:
        return "flat", atr_value

    if fast > slow:
        return "long", atr_value
    if fast < slow:
        return "short", atr_value
    return "flat", atr_value


def htf_trend(htf_ohlcv: List[List[float]], settings: Settings) -> Signal:
    """Return the higher-timeframe trend direction using the same EMA logic."""
    closes = [float(c[4]) for c in htf_ohlcv]
    if len(closes) < max(settings.fast_ema, settings.slow_ema) + 2:
        return "flat"
    fast = _ema(closes[-settings.fast_ema :], settings.fast_ema)
    slow = _ema(closes[-settings.slow_ema :], settings.slow_ema)
    if fast > slow:
        return "long"
    if fast < slow:
        return "short"
    return "flat"


def volume_ok(ohlcv: List[List[float]], period: int, min_mult: float) -> bool:
    """Return True if the latest candle's volume meets the minimum threshold."""
    if min_mult <= 0:
        return True
    if len(ohlcv) < period + 1:
        return True
    volumes = [float(c[5]) for c in ohlcv[-(period + 1) :]]
    avg_vol = sum(volumes[:-1]) / max(len(volumes) - 1, 1)
    if avg_vol <= 0:
        return True
    return volumes[-1] >= avg_vol * min_mult
