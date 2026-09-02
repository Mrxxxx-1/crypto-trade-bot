"""Tests for per-bar direction resolution (``DIRECTION_MODE``).

The invariant that matters most: an open position must never flip direction.
Re-resolving mid-trade would invert every stop and trail comparison, turning a
protective stop into a target.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import direction as direction_mod  # noqa: E402
from src.strategy_trend import LONG, SHORT, trend_signal  # noqa: E402
from tests.helpers import SYMBOL, make_settings, series  # noqa: E402

RALLY = series([100 + 3 * i for i in range(20)])
SELLOFF = series([160 - 3 * i for i in range(20)])
FLAT = series([100.0] * 20)


@dataclass
class FakePosition:
    """Stand-in for ``Position`` / ``BTPosition`` (both expose ``side``)."""

    side: str


class ConfigTest(unittest.TestCase):
    def test_default_is_static_so_existing_configs_are_unchanged(self):
        self.assertEqual(make_settings().direction_mode, "static")

    def test_signal_mode_parses(self):
        self.assertEqual(make_settings(DIRECTION_MODE="signal").direction_mode, "signal")

    def test_mode_is_case_insensitive_and_trimmed(self):
        self.assertEqual(make_settings(DIRECTION_MODE=" SIGNAL ").direction_mode, "signal")

    def test_typo_raises_rather_than_silently_falling_back(self):
        # A silent fallback would leave the bot pinned to SHORT_SYMBOLS while
        # the operator believed the trend was choosing.
        for bad in ("signl", "dynamic", "long"):
            with self.subTest(mode=bad), self.assertRaises(ValueError):
                make_settings(DIRECTION_MODE=bad)


class SignalDirectionTest(unittest.TestCase):
    def test_rally_reads_long(self):
        self.assertEqual(direction_mod.signal_direction(RALLY, make_settings()), LONG)

    def test_selloff_reads_short(self):
        self.assertEqual(direction_mod.signal_direction(SELLOFF, make_settings()), SHORT)

    def test_flat_market_is_undecided(self):
        self.assertIsNone(direction_mod.signal_direction(FLAT, make_settings()))

    def test_insufficient_data_is_undecided(self):
        self.assertIsNone(direction_mod.signal_direction(RALLY[:3], make_settings()))

    def test_never_contradicts_trend_signal(self):
        """The resolver must agree with the strategy it claims to read."""
        settings = make_settings()
        for name, window in (("rally", RALLY), ("selloff", SELLOFF), ("flat", FLAT)):
            with self.subTest(window=name):
                resolved = direction_mod.signal_direction(window, settings)
                if resolved is None:
                    self.assertFalse(trend_signal(window, settings, LONG))
                    self.assertFalse(trend_signal(window, settings, SHORT))
                else:
                    self.assertTrue(trend_signal(window, settings, resolved))


class StaticModeTest(unittest.TestCase):
    def test_static_ignores_the_trend_and_obeys_short_symbols(self):
        settings = make_settings(SHORT_SYMBOLS="BTC,ETH")
        # A textbook rally, yet static mode still insists on the short side.
        self.assertEqual(direction_mod.resolve(SYMBOL, settings, RALLY), SHORT)

    def test_static_defaults_to_long_when_not_listed(self):
        settings = make_settings(SHORT_SYMBOLS="")
        self.assertEqual(direction_mod.resolve(SYMBOL, settings, SELLOFF), LONG)

    def test_static_matches_direction_for_exactly(self):
        for shorts in ("", "BTC", "ETH", "BTC,ETH"):
            settings = make_settings(SHORT_SYMBOLS=shorts)
            for window in (RALLY, SELLOFF, FLAT):
                self.assertEqual(
                    direction_mod.resolve(SYMBOL, settings, window),
                    settings.direction_for(SYMBOL),
                )


class SignalModeTest(unittest.TestCase):
    def test_signal_mode_takes_the_long_side_in_a_rally_despite_short_symbols(self):
        """The whole point: SHORT_SYMBOLS no longer vetoes a valid long."""
        settings = make_settings(DIRECTION_MODE="signal", SHORT_SYMBOLS="BTC,ETH")
        self.assertEqual(direction_mod.resolve(SYMBOL, settings, RALLY), LONG)

    def test_signal_mode_takes_the_short_side_in_a_selloff(self):
        settings = make_settings(DIRECTION_MODE="signal", SHORT_SYMBOLS="")
        self.assertEqual(direction_mod.resolve(SYMBOL, settings, SELLOFF), SHORT)

    def test_undecided_falls_back_to_static_for_a_coherent_diagnostic(self):
        # Callers render "no_entry(<direction>)"; None would break that string.
        settings = make_settings(DIRECTION_MODE="signal", SHORT_SYMBOLS="BTC")
        self.assertEqual(direction_mod.resolve(SYMBOL, settings, FLAT), SHORT)
        settings = make_settings(DIRECTION_MODE="signal", SHORT_SYMBOLS="")
        self.assertEqual(direction_mod.resolve(SYMBOL, settings, FLAT), LONG)


class OpenPositionNeverFlipsTest(unittest.TestCase):
    """An open position keeps its entry direction in every mode."""

    def test_direction_of_maps_sides(self):
        self.assertEqual(direction_mod.direction_of(FakePosition("buy")), LONG)
        self.assertEqual(direction_mod.direction_of(FakePosition("sell")), SHORT)

    def test_side_casing_is_tolerated(self):
        self.assertEqual(direction_mod.direction_of(FakePosition("SELL")), SHORT)
        self.assertEqual(direction_mod.direction_of(FakePosition("Buy")), LONG)

    def test_open_short_survives_a_rally_in_signal_mode(self):
        """Regression guard: flipping here would invert the stop comparison."""
        settings = make_settings(DIRECTION_MODE="signal", SHORT_SYMBOLS="")
        self.assertEqual(
            direction_mod.resolve(SYMBOL, settings, RALLY, FakePosition("sell")),
            SHORT,
        )

    def test_open_long_survives_a_selloff_in_signal_mode(self):
        settings = make_settings(DIRECTION_MODE="signal", SHORT_SYMBOLS="BTC")
        self.assertEqual(
            direction_mod.resolve(SYMBOL, settings, SELLOFF, FakePosition("buy")),
            LONG,
        )

    def test_open_position_wins_over_static_config_too(self):
        settings = make_settings(SHORT_SYMBOLS="BTC,ETH")
        self.assertEqual(
            direction_mod.resolve(SYMBOL, settings, FLAT, FakePosition("buy")),
            LONG,
        )


if __name__ == "__main__":
    unittest.main()
