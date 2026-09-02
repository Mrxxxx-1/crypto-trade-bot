"""Tests for the shared indicators and entry-quality gates.

Two things worth stating, since both are load-bearing elsewhere:

* ``atr_series`` must agree with ``compute_atr`` on the final reading -- the
  live hedge sizes stops off the series and the trend strategy off the scalar,
  and a mismatch would make them silently disagree.
* Every gate returns True when its knob is disabled and False when there is not
  enough data. A gate that defaulted to True on thin data would let the bot
  enter on a cold start.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.indicators import (  # noqa: E402
    LONG,
    SHORT,
    adx_ok,
    atr_series,
    compute_adx,
    compute_atr,
    compute_ema,
    htf_trend_ok,
    percentile_rank,
    true_ranges,
    volume_ok,
)
from tests.helpers import bars, make_settings, series  # noqa: E402


def sawtooth(cycles: int = 6, leg: int = 8, step: float = 2.0) -> list[list]:
    """Range-bound zigzag: ``leg`` bars up, ``leg`` bars down, repeated.

    A naive one-bar alternation is useless here -- with a fixed spread every
    bar ends up with the same high and low, so Wilder sees *zero* directional
    movement and ADX reads 100 rather than 0. Multi-bar legs make both +DM and
    -DM accumulate so they genuinely cancel.
    """
    path: list[float] = []
    top = 100.0 + step * (leg - 1)
    for _ in range(cycles):
        path.extend(100.0 + step * i for i in range(leg))
        path.extend(top - step * i for i in range(leg))
    return series(path)


TRENDING = series([100 + 2 * i for i in range(80)])
CHOPPY = sawtooth()


class TrueRangeTest(unittest.TestCase):
    def test_uses_the_previous_close_on_a_gap(self) -> None:
        rows = [
            [0, 100.0, 101.0, 99.0, 100.0, 1.0],
            [1, 100.0, 106.0, 105.0, 105.0, 1.0],  # gap up: 106 - 100 dominates
        ]
        self.assertEqual(true_ranges(rows), [6.0])

    def test_is_empty_for_a_single_bar(self) -> None:
        self.assertEqual(true_ranges([[0, 1.0, 2.0, 0.5, 1.5, 1.0]]), [])


class AtrTest(unittest.TestCase):
    def test_atr_is_the_mean_of_recent_true_ranges(self) -> None:
        # Flat closes, so every true range equals the bar's spread.
        self.assertAlmostEqual(compute_atr(bars([2.0] * 10), 5), 2.0)

    def test_atr_needs_period_plus_one_bars(self) -> None:
        self.assertEqual(compute_atr(bars([1.0] * 5), 5), 0.0)

    def test_atr_series_is_a_rolling_mean(self) -> None:
        rows = bars([1.0, 1.0, 1.0, 3.0, 3.0])
        # 4 true ranges (1,1,3,3); period 2 -> means of (1,1), (1,3), (3,3)
        self.assertEqual(atr_series(rows, 2), [1.0, 2.0, 3.0])

    def test_atr_series_needs_a_full_period(self) -> None:
        self.assertEqual(atr_series(bars([1.0, 1.0]), 5), [])

    def test_atr_series_rejects_a_nonsense_period(self) -> None:
        self.assertEqual(atr_series(bars([1.0] * 10), 0), [])

    def test_final_series_reading_matches_compute_atr(self) -> None:
        """The hedge and the trend strategy must not silently disagree."""
        rows = series([100, 104, 99, 107, 103, 111, 108, 115, 110, 118, 112])
        for period in (2, 3, 5):
            with self.subTest(period=period):
                self.assertAlmostEqual(
                    atr_series(rows, period)[-1], compute_atr(rows, period), places=9
                )


class EmaTest(unittest.TestCase):
    def test_constant_series_returns_that_constant(self) -> None:
        self.assertAlmostEqual(compute_ema([5.0] * 20, 10), 5.0)

    def test_empty_or_bad_period_is_zero(self) -> None:
        self.assertEqual(compute_ema([], 10), 0.0)
        self.assertEqual(compute_ema([1.0, 2.0], 0), 0.0)

    def test_a_fast_ema_tracks_price_more_closely_than_a_slow_one(self) -> None:
        rising = [float(i) for i in range(50)]
        self.assertGreater(compute_ema(rising, 3), compute_ema(rising, 30))


class AdxTest(unittest.TestCase):
    def test_flat_market_has_no_measurable_trend(self) -> None:
        self.assertEqual(compute_adx(bars([1.0] * 60), 14), 0.0)

    def test_thin_history_is_zero(self) -> None:
        self.assertEqual(compute_adx(series([1, 2, 3]), 14), 0.0)

    def test_a_steady_trend_scores_higher_than_chop(self) -> None:
        self.assertGreater(compute_adx(TRENDING, 14), compute_adx(CHOPPY, 14))


class PercentileRankTest(unittest.TestCase):
    def test_counts_values_at_or_below(self) -> None:
        history = [1.0, 2.0, 3.0, 4.0]
        self.assertAlmostEqual(percentile_rank(history, 1.0), 25.0)
        self.assertAlmostEqual(percentile_rank(history, 4.0), 100.0)

    def test_empty_history_is_undefined(self) -> None:
        self.assertIsNone(percentile_rank([], 1.0))

    def test_a_value_below_everything_ranks_zero(self) -> None:
        self.assertAlmostEqual(percentile_rank([5.0, 6.0], 1.0), 0.0)


class GateTest(unittest.TestCase):
    """Each gate: disabled -> True, thin data -> False."""

    def test_adx_gate_disabled_lets_everything_through(self) -> None:
        settings = make_settings(ADX_MIN=0)
        self.assertTrue(adx_ok(bars([1.0] * 5), settings))

    def test_adx_gate_blocks_when_undefined(self) -> None:
        settings = make_settings(ADX_MIN=25, ADX_PERIOD=14)
        self.assertFalse(adx_ok(series([1, 2, 3]), settings))

    def test_adx_gate_blocks_chop_and_admits_a_trend(self) -> None:
        settings = make_settings(ADX_MIN=25, ADX_PERIOD=14)
        self.assertTrue(adx_ok(TRENDING, settings))
        self.assertFalse(adx_ok(CHOPPY, settings))

    def test_volume_gate_disabled_lets_everything_through(self) -> None:
        self.assertTrue(volume_ok(series([1, 2, 3]), make_settings(VOLUME_MIN_MULT=0)))

    def test_volume_gate_compares_against_its_moving_average(self) -> None:
        settings = make_settings(VOLUME_MIN_MULT=1.5, VOLUME_MA_PERIOD=3)
        rows = series([100] * 5)
        for row in rows:
            row[5] = 100.0
        self.assertFalse(volume_ok(rows, settings))
        rows[-1][5] = 1000.0
        self.assertTrue(volume_ok(rows, settings))

    def test_volume_gate_blocks_on_thin_data(self) -> None:
        settings = make_settings(VOLUME_MIN_MULT=1.5, VOLUME_MA_PERIOD=20)
        self.assertFalse(volume_ok(series([1, 2, 3]), settings))

    def test_htf_gate_disabled_lets_everything_through(self) -> None:
        self.assertTrue(htf_trend_ok([], make_settings(MTF_ENABLED="false")))

    def test_htf_gate_blocks_on_thin_data(self) -> None:
        settings = make_settings(MTF_ENABLED="true", MTF_EMA_PERIOD=50)
        self.assertFalse(htf_trend_ok(series([1, 2, 3]), settings))

    def test_htf_gate_mirrors_for_shorts(self) -> None:
        settings = make_settings(MTF_ENABLED="true", MTF_EMA_PERIOD=5)
        rising = series([100 + 5 * i for i in range(30)])
        self.assertTrue(htf_trend_ok(rising, settings, LONG))
        self.assertFalse(htf_trend_ok(rising, settings, SHORT))


if __name__ == "__main__":
    unittest.main()
