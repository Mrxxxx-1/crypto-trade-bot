"""Shared fixtures for the test suite: settings builders and candle generators.

``make_settings`` deliberately ignores the developer's real ``.env`` so tests
never depend on local configuration (or accidentally read live credentials).
The EMA/ATR periods are short so synthetic series stay small enough to reason
about by hand.
"""

from __future__ import annotations

import os
from unittest import mock

from src.config import Settings, load_settings

SYMBOL = "BTC/USDC:USDC"
STEP_MS = 4 * 3600 * 1000

BASE_ENV = {
    "SYMBOLS": SYMBOL,
    "SHORT_SYMBOLS": "",
    "STRATEGY": "trend",
    "TIMEFRAME": "4h",
    "LOOKBACK_CANDLES": "30",
    "INITIAL_EQUITY": "1000",
    "MAX_LEVERAGE": "3",
    "RISK_PER_TRADE_PCT": "1.0",
    "FAST_EMA": "3",
    "SLOW_EMA": "5",
    "TREND_EMA_PERIOD": "5",
    "ATR_PERIOD": "5",
    "STOP_ATR_MULTIPLIER": "3.0",
    "TRAIL_ATR_MULTIPLIER": "6.0",
    "ADX_PERIOD": "5",
    "ADX_MIN": "25",
    "VOLUME_MIN_MULT": "0",
    "MTF_ENABLED": "false",
    "ENTRY_FEE_BPS": "2",
    "EXIT_FEE_BPS": "3.5",
    "SLIPPAGE_BPS": "0",
    "MAX_DAILY_LOSS_PCT": "100",
    "MAX_CONSECUTIVE_LOSSES": "99",
}


def make_settings(**overrides) -> Settings:
    """Settings built from documented defaults, ignoring the developer's .env."""
    env = dict(BASE_ENV)
    env.update({k: str(v) for k, v in overrides.items()})
    with mock.patch("src.config.load_dotenv", lambda *a, **k: None), \
            mock.patch.dict(os.environ, env, clear=True):
        return load_settings()


def series(closes, ts0: int = 0, spread: float = 0.5) -> list[list]:
    """OHLCV rows from a close path; ``spread`` keeps true range (and ATR) non-zero."""
    rows = []
    prev = closes[0]
    for i, close in enumerate(closes):
        rows.append(
            [
                ts0 + i * STEP_MS,
                prev,
                max(prev, close) + spread,
                min(prev, close) - spread,
                close,
                100.0,
            ]
        )
        prev = close
    return rows


def bars(spreads: list[float], price: float = 100.0) -> list[list]:
    """Flat-close bars whose per-bar range equals the given spread.

    Close never moves, so true range is exactly the spread. This isolates ATR
    from any directional component.
    """
    return [
        [i * 3600_000, price, price + s / 2, price - s / 2, price, 100.0]
        for i, s in enumerate(spreads)
    ]
