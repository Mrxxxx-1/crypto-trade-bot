"""Volatility-compression ("squeeze") detection, direction-neutral and I/O-free.

A squeeze is the setup the straddle is meant to exploit: realized volatility has
collapsed into a tight coil, so the next directional expansion is likely to be
large relative to the stop distance you can currently afford.

Three independent measures, each expressed as a *percentile rank against the
symbol's own recent history* so the thresholds are scale-free and work equally on
BTC at $100k and ETH at $3k:

* ATR rank        - current ATR versus the last ``lookback`` ATR readings
* Band-width rank - current Bollinger band width versus its own history
* NR-k            - the latest bar is the narrowest of the last ``k`` bars

Used by ``src/strategy_straddle.py`` (``--entry-mode squeeze``) and intended to
be shared by any live hedge trigger.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean, pstdev
from typing import List, Optional, Sequence

COMBINE_MODES = ("any", "all")


@dataclass(frozen=True)
class SqueezeConfig:
    """Thresholds for calling the market compressed.

    ``atr_pct_max`` and ``bbw_pct_max`` are percentile ceilings: 20.0 means "in
    the calmest 20% of the recent past". Set either to 0 to disable that check.
    """

    lookback: int = 120
    atr_period: int = 14
    atr_pct_max: float = 20.0
    bb_period: int = 20
    bb_stdev: float = 2.0
    bbw_pct_max: float = 20.0
    nr_lookback: int = 0
    combine: str = "all"

    def validate(self) -> None:
        if self.combine not in COMBINE_MODES:
            raise ValueError(f"combine must be one of {COMBINE_MODES}, got {self.combine!r}")
        if self.lookback < 2:
            raise ValueError("lookback must be >= 2 to rank against history")
        if self.atr_period < 1 or self.bb_period < 2:
            raise ValueError("atr_period must be >= 1 and bb_period >= 2")
        for name in ("atr_pct_max", "bbw_pct_max"):
            value = getattr(self, name)
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be between 0 and 100, got {value}")
        if not self.enabled_checks():
            raise ValueError("SqueezeConfig disables every check; nothing would ever be compressed")

    def enabled_checks(self) -> tuple[str, ...]:
        checks = []
        if self.atr_pct_max > 0:
            checks.append("atr")
        if self.bbw_pct_max > 0:
            checks.append("bbw")
        if self.nr_lookback > 0:
            checks.append("nr")
        return tuple(checks)

    def describe(self) -> str:
        parts = []
        if self.atr_pct_max > 0:
            parts.append(f"atr<=p{self.atr_pct_max:g}")
        if self.bbw_pct_max > 0:
            parts.append(f"bbw<=p{self.bbw_pct_max:g}")
        if self.nr_lookback > 0:
            parts.append(f"nr{self.nr_lookback}")
        return f"squeeze({self.combine}: {', '.join(parts)}, lookback={self.lookback})"


@dataclass(frozen=True)
class SqueezeState:
    """What the detector saw, so a log line or Telegram alert can explain itself."""

    compressed: bool
    atr_rank: Optional[float] = None
    bbw_rank: Optional[float] = None
    narrowest_of: Optional[int] = None
    reason: str = ""

    def summary(self) -> str:
        bits = []
        if self.atr_rank is not None:
            bits.append(f"atr p{self.atr_rank:.0f}")
        if self.bbw_rank is not None:
            bits.append(f"bbw p{self.bbw_rank:.0f}")
        if self.narrowest_of is not None:
            bits.append(f"nr{self.narrowest_of}")
        detail = ", ".join(bits) if bits else "insufficient data"
        return f"{'compressed' if self.compressed else 'not compressed'} ({detail})"


def true_ranges(ohlcv: Sequence[Sequence[float]]) -> List[float]:
    """Wilder true range for every bar after the first."""
    out: List[float] = []
    for i in range(1, len(ohlcv)):
        high = float(ohlcv[i][2])
        low = float(ohlcv[i][3])
        prev_close = float(ohlcv[i - 1][4])
        out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return out


def atr_series(ohlcv: Sequence[Sequence[float]], period: int) -> List[float]:
    """Rolling ATR readings, using the same SMA-of-true-range as ``compute_atr``."""
    if period < 1:
        return []
    trs = true_ranges(ohlcv)
    if len(trs) < period:
        return []
    return [fmean(trs[i - period : i]) for i in range(period, len(trs) + 1)]


def band_width(closes: Sequence[float], stdev_mult: float) -> Optional[float]:
    """Bollinger band width as a fraction of the mean, or None if undefined."""
    if len(closes) < 2:
        return None
    mid = fmean(closes)
    if mid <= 0:
        return None
    return (2.0 * stdev_mult * pstdev(closes)) / mid


def band_width_series(closes: Sequence[float], period: int, stdev_mult: float) -> List[float]:
    if period < 2 or len(closes) < period:
        return []
    out: List[float] = []
    for i in range(period, len(closes) + 1):
        width = band_width(closes[i - period : i], stdev_mult)
        if width is not None:
            out.append(width)
    return out


def percentile_rank(history: Sequence[float], value: float) -> Optional[float]:
    """Share of ``history`` at or below ``value``, as a percentage.

    A rank of 0 means nothing in the sample was calmer; 100 means nothing was
    more volatile.
    """
    if not history:
        return None
    at_or_below = sum(1 for h in history if h <= value)
    return 100.0 * at_or_below / len(history)


def squeeze_state(closed_ohlcv: Sequence[Sequence[float]], cfg: SqueezeConfig) -> SqueezeState:
    """Evaluate compression on closed bars only.

    Returns ``compressed=False`` when there is not enough history to rank
    against, so a cold start never fires a hedge on thin data.
    """
    cfg.validate()

    atr_rank: Optional[float] = None
    if cfg.atr_pct_max > 0:
        atrs = atr_series(closed_ohlcv, cfg.atr_period)
        window = atrs[-cfg.lookback :]
        if len(window) >= cfg.lookback:
            atr_rank = percentile_rank(window, window[-1])

    bbw_rank: Optional[float] = None
    if cfg.bbw_pct_max > 0:
        closes = [float(c[4]) for c in closed_ohlcv]
        widths = band_width_series(closes, cfg.bb_period, cfg.bb_stdev)
        window = widths[-cfg.lookback :]
        if len(window) >= cfg.lookback:
            bbw_rank = percentile_rank(window, window[-1])

    nr_ok: Optional[bool] = None
    if cfg.nr_lookback > 0:
        if len(closed_ohlcv) >= cfg.nr_lookback:
            recent = closed_ohlcv[-cfg.nr_lookback :]
            ranges = [float(c[2]) - float(c[3]) for c in recent]
            nr_ok = ranges[-1] <= min(ranges)

    results: dict[str, bool] = {}
    if atr_rank is not None:
        results["atr"] = atr_rank <= cfg.atr_pct_max
    if bbw_rank is not None:
        results["bbw"] = bbw_rank <= cfg.bbw_pct_max
    if nr_ok is not None:
        results["nr"] = nr_ok

    enabled = cfg.enabled_checks()
    missing = [name for name in enabled if name not in results]
    if missing:
        return SqueezeState(
            compressed=False,
            atr_rank=atr_rank,
            bbw_rank=bbw_rank,
            narrowest_of=cfg.nr_lookback if cfg.nr_lookback > 0 else None,
            reason=f"insufficient history for: {', '.join(missing)}",
        )

    values = [results[name] for name in enabled]
    compressed = all(values) if cfg.combine == "all" else any(values)
    passed = [name for name in enabled if results[name]]
    return SqueezeState(
        compressed=compressed,
        atr_rank=atr_rank,
        bbw_rank=bbw_rank,
        narrowest_of=cfg.nr_lookback if cfg.nr_lookback > 0 else None,
        reason=f"{cfg.combine}({','.join(enabled)}); passed: {','.join(passed) or 'none'}",
    )


def is_compressed(closed_ohlcv: Sequence[Sequence[float]], cfg: SqueezeConfig) -> bool:
    return squeeze_state(closed_ohlcv, cfg).compressed
