"""Tests for volatility-compression detection and the squeeze straddle entry mode.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategy_squeeze import (  # noqa: E402
    SqueezeConfig,
    atr_series,
    band_width,
    band_width_series,
    is_compressed,
    percentile_rank,
    squeeze_state,
    true_ranges,
)
from src.strategy_straddle import should_open_pair  # noqa: E402
from tests.test_straddle import make_settings, series  # noqa: E402


def bars(spreads: list[float], price: float = 100.0) -> list[list]:
    """Flat-close bars whose per-bar range equals the given spread.

    Close never moves, so true range is exactly the spread and the Bollinger
    width is zero — this isolates the ATR measure.
    """
    return [
        [i * 3600_000, price, price + s / 2, price - s / 2, price, 100.0]
        for i, s in enumerate(spreads)
    ]


class HelperTest(unittest.TestCase):
    def test_true_range_uses_the_previous_close(self) -> None:
        rows = [
            [0, 100.0, 101.0, 99.0, 100.0, 1.0],
            [1, 100.0, 106.0, 105.0, 105.0, 1.0],  # gap up: 106 - 100 dominates
        ]
        self.assertEqual(true_ranges(rows), [6.0])

    def test_atr_series_is_a_rolling_mean_of_true_range(self) -> None:
        rows = bars([1.0, 1.0, 1.0, 3.0, 3.0])
        # 4 true ranges (1,1,3,3); period 2 -> means of (1,1), (1,3), (3,3)
        self.assertEqual(atr_series(rows, 2), [1.0, 2.0, 3.0])

    def test_atr_series_needs_a_full_period(self) -> None:
        self.assertEqual(atr_series(bars([1.0, 1.0]), 5), [])

    def test_band_width_is_scale_free(self) -> None:
        cheap = band_width([9.0, 10.0, 11.0], 2.0)
        pricey = band_width([900.0, 1000.0, 1100.0], 2.0)
        self.assertAlmostEqual(cheap, pricey)

    def test_band_width_is_zero_for_a_constant_series(self) -> None:
        self.assertAlmostEqual(band_width([100.0] * 10, 2.0), 0.0)

    def test_band_width_series_length(self) -> None:
        self.assertEqual(len(band_width_series([100.0] * 10, 4, 2.0)), 7)

    def test_percentile_rank_counts_values_at_or_below(self) -> None:
        history = [1.0, 2.0, 3.0, 4.0]
        self.assertAlmostEqual(percentile_rank(history, 1.0), 25.0)
        self.assertAlmostEqual(percentile_rank(history, 4.0), 100.0)
        self.assertIsNone(percentile_rank([], 1.0))


class SqueezeStateTest(unittest.TestCase):
    def test_calm_tail_is_compressed(self) -> None:
        cfg = SqueezeConfig(lookback=20, atr_period=2, atr_pct_max=20.0, bbw_pct_max=0)
        # Wide for a long stretch, then the calmest bars at the very end.
        rows = bars([5.0] * 40 + [0.1] * 4)
        state = squeeze_state(rows, cfg)
        self.assertTrue(state.compressed)
        self.assertLessEqual(state.atr_rank, 20.0)

    def test_volatile_tail_is_not_compressed(self) -> None:
        cfg = SqueezeConfig(lookback=20, atr_period=2, atr_pct_max=20.0, bbw_pct_max=0)
        rows = bars([0.1] * 40 + [5.0] * 4)
        state = squeeze_state(rows, cfg)
        self.assertFalse(state.compressed)
        self.assertGreater(state.atr_rank, 20.0)

    def test_thin_history_never_fires(self) -> None:
        cfg = SqueezeConfig(lookback=200, atr_period=2, atr_pct_max=20.0, bbw_pct_max=0)
        state = squeeze_state(bars([1.0] * 30), cfg)
        self.assertFalse(state.compressed)
        self.assertIn("insufficient history", state.reason)

    def test_combine_all_requires_every_enabled_check(self) -> None:
        # ATR is calm at the end, but the range is not the narrowest of 3.
        rows = bars([5.0] * 40 + [0.1, 0.1, 0.5])
        calm_atr = SqueezeConfig(lookback=20, atr_period=2, atr_pct_max=30.0, bbw_pct_max=0)
        self.assertTrue(is_compressed(rows, calm_atr))

        both = SqueezeConfig(
            lookback=20, atr_period=2, atr_pct_max=30.0, bbw_pct_max=0,
            nr_lookback=3, combine="all",
        )
        self.assertFalse(is_compressed(rows, both))

        either = SqueezeConfig(
            lookback=20, atr_period=2, atr_pct_max=30.0, bbw_pct_max=0,
            nr_lookback=3, combine="any",
        )
        self.assertTrue(is_compressed(rows, either))

    def test_nr_check_fires_on_the_narrowest_bar(self) -> None:
        cfg = SqueezeConfig(atr_pct_max=0, bbw_pct_max=0, nr_lookback=5, lookback=2)
        self.assertTrue(is_compressed(bars([3.0, 3.0, 3.0, 3.0, 0.5]), cfg))
        self.assertFalse(is_compressed(bars([3.0, 3.0, 3.0, 0.5, 3.0]), cfg))

    def test_band_width_check_detects_a_coiling_price(self) -> None:
        cfg = SqueezeConfig(lookback=20, bb_period=5, bbw_pct_max=20.0, atr_pct_max=0)
        wide = [100.0 + (10 if i % 2 else -10) for i in range(40)]
        tight = [100.0 + (0.05 if i % 2 else -0.05) for i in range(6)]
        self.assertTrue(is_compressed(series(wide + tight), cfg))
        self.assertFalse(is_compressed(series(tight + wide), cfg))

    def test_state_summary_is_human_readable(self) -> None:
        cfg = SqueezeConfig(lookback=20, atr_period=2, atr_pct_max=20.0, bbw_pct_max=0)
        summary = squeeze_state(bars([5.0] * 40 + [0.1] * 4), cfg).summary()
        self.assertIn("compressed", summary)
        self.assertIn("atr p", summary)


class SqueezeConfigTest(unittest.TestCase):
    def test_rejects_a_config_with_every_check_disabled(self) -> None:
        with self.assertRaises(ValueError):
            SqueezeConfig(atr_pct_max=0, bbw_pct_max=0, nr_lookback=0).validate()

    def test_rejects_bad_thresholds_and_windows(self) -> None:
        with self.assertRaises(ValueError):
            SqueezeConfig(combine="sometimes").validate()
        with self.assertRaises(ValueError):
            SqueezeConfig(lookback=1).validate()
        with self.assertRaises(ValueError):
            SqueezeConfig(atr_pct_max=150).validate()
        with self.assertRaises(ValueError):
            SqueezeConfig(bb_period=1).validate()

    def test_describe_lists_the_enabled_checks(self) -> None:
        text = SqueezeConfig(nr_lookback=7).describe()
        self.assertIn("atr<=p20", text)
        self.assertIn("bbw<=p20", text)
        self.assertIn("nr7", text)


class SqueezeEntryModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings()

    def test_squeeze_entry_mode_gates_on_compression(self) -> None:
        cfg = SqueezeConfig(lookback=20, atr_period=2, atr_pct_max=20.0, bbw_pct_max=0)
        calm = bars([5.0] * 40 + [0.1] * 4)
        loud = bars([0.1] * 40 + [5.0] * 4)
        self.assertTrue(should_open_pair(calm, self.settings, "squeeze", cfg))
        self.assertFalse(should_open_pair(loud, self.settings, "squeeze", cfg))

    def test_squeeze_mode_falls_back_to_default_config(self) -> None:
        # No config passed: defaults need 120 bars of history, so thin data is a no.
        self.assertFalse(should_open_pair(bars([1.0] * 30), self.settings, "squeeze"))

    def test_other_entry_modes_ignore_the_squeeze_config(self) -> None:
        loud = bars([0.1] * 40 + [5.0] * 4)
        self.assertTrue(should_open_pair(loud, self.settings, "always"))


if __name__ == "__main__":
    unittest.main()
