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
