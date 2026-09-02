"""Dual-leg straddle decision rules (simulation only), direction-neutral.

The straddle opens a mirrored long and short on the same symbol with identical
size and identical ATR stop distance, waits for a trend to declare itself, then
cuts the losing leg and lets the winner ride the usual chandelier trail.

Pure functions, no I/O, mirroring the style of ``src/strategy_trend.py``. The
engine that drives them lives in ``src/backtest_straddle.py``; nothing here is
wired into live trading, because Hyperliquid nets to one position per coin.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .config import Settings
from .strategy import adx_ok
from .strategy_squeeze import SqueezeConfig, is_compressed
from .strategy_trend import LONG, SHORT, trend_signal

ENTRY_MODES = ("always", "chop", "squeeze")
TRIGGERS = ("trend_signal", "atr", "range")


@dataclass(frozen=True)
class StraddleConfig:
    """Which entry rule opens the pair and which trigger picks the winner."""

    entry_mode: str = "always"
    trigger: str = "trend_signal"
    trigger_atr_mult: float = 1.0
    range_lookback: int = 20
    squeeze: SqueezeConfig = field(default_factory=SqueezeConfig)

    def validate(self) -> None:
        if self.entry_mode not in ENTRY_MODES:
            raise ValueError(f"entry_mode must be one of {ENTRY_MODES}, got {self.entry_mode!r}")
        if self.trigger not in TRIGGERS:
            raise ValueError(f"trigger must be one of {TRIGGERS}, got {self.trigger!r}")
        if self.trigger == "atr" and self.trigger_atr_mult <= 0:
            raise ValueError("trigger_atr_mult must be > 0 for the atr trigger")
        if self.trigger == "range" and self.range_lookback < 1:
            raise ValueError("range_lookback must be >= 1 for the range trigger")
        if self.entry_mode == "squeeze":
            self.squeeze.validate()

    def describe(self) -> str:
        if self.trigger == "atr":
            detail = f"atr x{self.trigger_atr_mult:g}"
        elif self.trigger == "range":
            detail = f"range {self.range_lookback} bars"
        else:
            detail = "trend_signal"
        entry = self.entry_mode
        if self.entry_mode == "squeeze":
            entry = self.squeeze.describe()
        return f"entry={entry}, trigger={detail}"


def opposite(direction: str) -> str:
    return SHORT if direction == LONG else LONG


def should_open_pair(
    closed_ohlcv: List[List[float]],
    settings: Settings,
    entry_mode: str = "always",
    squeeze_cfg: Optional[SqueezeConfig] = None,
) -> bool:
    """Whether to open a fresh pair on this bar, given we are flat.

    ``always`` re-opens whenever both legs are closed.

    ``chop`` is the exact inverse of the live chop filter: straddle only where
    ``adx_ok`` would keep the directional strategy out, on the theory that the
    pair pays off when a range finally breaks. An undefined ADX counts as chop,
    matching how ``adx_ok`` treats it as "not tradeable directionally".

    ``squeeze`` requires realized volatility to be compressed into the calmest
    tail of its own recent history — the coil an expansion breaks out of.
    """
    if entry_mode == "always":
        return True
    if entry_mode == "chop":
        if settings.adx_min <= 0:
            return True  # chop filter disabled; there is nothing to invert
        return not adx_ok(closed_ohlcv, settings)
    if entry_mode == "squeeze":
        return is_compressed(closed_ohlcv, squeeze_cfg or SqueezeConfig())
    raise ValueError(f"entry_mode must be one of {ENTRY_MODES}, got {entry_mode!r}")


def _pick(up_excursion: float, down_excursion: float, threshold: float) -> Optional[str]:
    """Resolve which side won, given how far the bar ran each way.

    When one bar clears the threshold in *both* directions the true sequence is
    unknowable from OHLC alone, so we take the larger excursion and abstain on an
    exact tie rather than guessing.
    """
    up_hit = up_excursion > 0 and up_excursion >= threshold
    down_hit = down_excursion > 0 and down_excursion >= threshold
    if up_hit and down_hit:
        if up_excursion > down_excursion:
            return LONG
        if down_excursion > up_excursion:
            return SHORT
        return None
    if up_hit:
        return LONG
    if down_hit:
        return SHORT
    return None


def _range_bounds(closed_ohlcv: List[List[float]], lookback: int) -> Optional[tuple[float, float]]:
    window = closed_ohlcv[-lookback:]
    if len(window) < lookback:
        return None
    return max(float(c[2]) for c in window), min(float(c[3]) for c in window)


def winner_direction(
    closed_ohlcv: List[List[float]],
    settings: Settings,
    cfg: StraddleConfig,
    ref_price: float,
    high: float,
    low: float,
    atr: float,
) -> Optional[str]:
    """Which leg the trend has declared the winner, or ``None`` to keep waiting.

    ``closed_ohlcv`` excludes the bar being acted on; ``high``/``low`` are that
    bar's extremes and ``ref_price`` is the price the pair was opened at.
    """
    if cfg.trigger == "trend_signal":
        if trend_signal(closed_ohlcv, settings, LONG):
            return LONG
        if trend_signal(closed_ohlcv, settings, SHORT):
            return SHORT
        return None

    if cfg.trigger == "atr":
        if atr <= 0 or ref_price <= 0:
            return None
        return _pick(high - ref_price, ref_price - low, cfg.trigger_atr_mult * atr)

    if cfg.trigger == "range":
        bounds = _range_bounds(closed_ohlcv, cfg.range_lookback)
        if bounds is None:
            return None
        prior_high, prior_low = bounds
        return _pick(high - prior_high, prior_low - low, 0.0)

    raise ValueError(f"trigger must be one of {TRIGGERS}, got {cfg.trigger!r}")


def trigger_level(
    closed_ohlcv: List[List[float]],
    cfg: StraddleConfig,
    ref_price: float,
    winner: str,
    atr: float,
) -> float:
    """Price at which the losing leg is cut, or 0.0 to cut at the bar close.

    The ATR and range triggers fire on an intrabar touch, so the loser is cut at
    the level that was touched — the same convention the engine uses for stops.
    ``trend_signal`` is evaluated on closed bars only, so it cuts at the close.
    """
    if cfg.trigger == "atr":
        if atr <= 0 or ref_price <= 0:
            return 0.0
        dist = cfg.trigger_atr_mult * atr
        return ref_price + dist if winner == LONG else ref_price - dist

    if cfg.trigger == "range":
        bounds = _range_bounds(closed_ohlcv, cfg.range_lookback)
        if bounds is None:
            return 0.0
        prior_high, prior_low = bounds
        return prior_high if winner == LONG else prior_low

    return 0.0
