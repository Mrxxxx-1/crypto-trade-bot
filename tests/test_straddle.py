"""Tests for the dual-leg straddle: triggers, symmetric sizing, and fee algebra.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backtest_straddle import (  # noqa: E402
    CUT_BY_TRIGGER,
    run_straddle_backtest,
)
from src.config import Settings, load_settings  # noqa: E402
from src.strategy import adx_ok  # noqa: E402
from src.strategy_straddle import (  # noqa: E402
    StraddleConfig,
    should_open_pair,
    trigger_level,
    winner_direction,
)
from src.strategy_trend import atr_value  # noqa: E402

SYMBOL = "BTC/USDC:USDC"
STEP_MS = 4 * 3600 * 1000

# Short EMA/ATR periods keep the synthetic series small enough to reason about.
BASE_ENV = {
    "SYMBOLS": SYMBOL,
    "SHORT_SYMBOLS": "",
    "STRATEGY": "trend",
    "TIMEFRAME": "4h",
    "LOOKBACK_CANDLES": "30",
    "HIGH_LOOKBACK_CANDLES": "5",
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


class ShouldOpenPairTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings()

    def test_always_mode_opens_whenever_flat(self) -> None:
        self.assertTrue(should_open_pair(series([100.0] * 20), self.settings, "always"))

    def test_chop_mode_is_the_inverse_of_the_live_chop_filter(self) -> None:
        choppy = series([100.0, 100.4] * 15)
        trending = series([100.0 + i * 3 for i in range(30)])
        for window in (choppy, trending):
            self.assertEqual(
                should_open_pair(window, self.settings, "chop"),
                not adx_ok(window, self.settings),
            )
        self.assertFalse(should_open_pair(trending, self.settings, "chop"))

    def test_chop_mode_straddles_a_dead_flat_market(self) -> None:
        # ADX is undefined with no directional movement; adx_ok treats that as
        # untradeable, so the straddle treats it as maximum chop.
        self.assertTrue(should_open_pair(series([100.0] * 30), self.settings, "chop"))

    def test_chop_mode_is_disabled_when_adx_min_is_zero(self) -> None:
        settings = make_settings(ADX_MIN="0")
        trending = series([100.0 + i * 3 for i in range(30)])
        self.assertTrue(should_open_pair(trending, settings, "chop"))

    def test_unknown_entry_mode_raises(self) -> None:
        with self.assertRaises(ValueError):
            should_open_pair(series([100.0] * 20), self.settings, "sideways")


class TriggerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings()

    def test_trend_signal_picks_the_side_the_emas_agree_on(self) -> None:
        cfg = StraddleConfig(trigger="trend_signal")
        up = series([100.0 + i for i in range(20)])
        down = series([100.0 - i for i in range(20)])
        flat = series([100.0] * 20)
        kwargs = dict(ref_price=100.0, high=101.0, low=99.0, atr=1.0)
        self.assertEqual(winner_direction(up, self.settings, cfg, **kwargs), "long")
        self.assertEqual(winner_direction(down, self.settings, cfg, **kwargs), "short")
        self.assertIsNone(winner_direction(flat, self.settings, cfg, **kwargs))

    def test_atr_trigger_fires_when_a_leg_is_k_atrs_offside(self) -> None:
        cfg = StraddleConfig(trigger="atr", trigger_atr_mult=1.5)
        window = series([100.0] * 20)
        call = lambda high, low: winner_direction(  # noqa: E731
            window, self.settings, cfg, 100.0, high, low, atr=2.0
        )
        self.assertEqual(call(103.5, 99.0), "long")   # +3.5 clears 1.5 x 2.0
        self.assertEqual(call(101.0, 96.5), "short")  # -3.5 clears it downward
        self.assertIsNone(call(102.9, 97.1))          # neither side clears 3.0

    def test_atr_trigger_takes_the_larger_excursion_and_abstains_on_a_tie(self) -> None:
        cfg = StraddleConfig(trigger="atr", trigger_atr_mult=1.0)
        window = series([100.0] * 20)
        self.assertEqual(
            winner_direction(window, self.settings, cfg, 100.0, 105.0, 97.0, atr=1.0),
            "long",
        )
        self.assertIsNone(
            winner_direction(window, self.settings, cfg, 100.0, 105.0, 95.0, atr=1.0)
        )

    def test_range_trigger_fires_on_a_break_of_the_lookback_extreme(self) -> None:
        cfg = StraddleConfig(trigger="range", range_lookback=10)
        window = series([100.0] * 20)  # prior high 100.5, prior low 99.5
        call = lambda high, low: winner_direction(  # noqa: E731
            window, self.settings, cfg, 100.0, high, low, atr=1.0
        )
        self.assertEqual(call(101.0, 99.8), "long")
        self.assertEqual(call(100.2, 99.0), "short")
        self.assertIsNone(call(100.4, 99.6))

    def test_range_trigger_needs_a_full_lookback_window(self) -> None:
        cfg = StraddleConfig(trigger="range", range_lookback=30)
        self.assertIsNone(
            winner_direction(series([100.0] * 10), self.settings, cfg, 100.0, 200.0, 99.0, 1.0)
        )

    def test_trigger_level_is_the_touched_price(self) -> None:
        window = series([100.0] * 20)
        atr_cfg = StraddleConfig(trigger="atr", trigger_atr_mult=1.5)
        self.assertAlmostEqual(trigger_level(window, atr_cfg, 100.0, "long", 2.0), 103.0)
        self.assertAlmostEqual(trigger_level(window, atr_cfg, 100.0, "short", 2.0), 97.0)

        range_cfg = StraddleConfig(trigger="range", range_lookback=10)
        self.assertAlmostEqual(trigger_level(window, range_cfg, 100.0, "long", 1.0), 100.5)
        self.assertAlmostEqual(trigger_level(window, range_cfg, 100.0, "short", 1.0), 99.5)

        trend_cfg = StraddleConfig(trigger="trend_signal")
        self.assertEqual(trigger_level(window, trend_cfg, 100.0, "long", 1.0), 0.0)

    def test_invalid_config_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            StraddleConfig(trigger="vibes").validate()
        with self.assertRaises(ValueError):
            StraddleConfig(trigger="atr", trigger_atr_mult=0).validate()
        with self.assertRaises(ValueError):
            StraddleConfig(trigger="range", range_lookback=0).validate()


# ---------------------------------------------------------------------------
# Engine scenarios
# ---------------------------------------------------------------------------

# 35 flat bars at 100 (ATR settles at exactly 1.0), then a 30-bar rally in +1
# steps (ATR settles at 2.0), then a crash through the 6-ATR chandelier. The
# pair opens on bar 30, the first bar the lookback window allows, and the rally
# is long enough that the trail ends up above the entry.
FLAT_BARS = 35
RALLY = [100.0 + i for i in range(1, 31)]
SLIDE = [100.0 - i for i in range(1, 31)]
CRASH = [80.0]
SPIKE = [130.0]
PAIR_ENTRY_PRICE = 100.0

RALLY_PATH = [100.0] * FLAT_BARS + RALLY + CRASH
SLIDE_PATH = [100.0] * FLAT_BARS + SLIDE + SPIKE


def straddle_run(settings: Settings, cfg: StraddleConfig, closes: list[float]):
    return run_straddle_backtest(settings, {SYMBOL: series(closes)}, cfg)


class StraddleEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings()

    def test_both_legs_open_at_the_same_size_and_price(self) -> None:
        cfg = StraddleConfig(trigger="atr", trigger_atr_mult=1.0)
        result = straddle_run(self.settings, cfg, RALLY_PATH)
        self.assertEqual(len(result.pairs), 1)
        long_trade, short_trade = self._legs(result)
        self.assertAlmostEqual(long_trade.size, short_trade.size)
        self.assertAlmostEqual(long_trade.entry_price, short_trade.entry_price)
        self.assertAlmostEqual(long_trade.entry_price, PAIR_ENTRY_PRICE)

    def test_slippage_keeps_sizes_equal_and_entries_symmetric(self) -> None:
        settings = make_settings(SLIPPAGE_BPS="10")
        cfg = StraddleConfig(trigger="atr", trigger_atr_mult=1.0)
        result = straddle_run(settings, cfg, RALLY_PATH)
        long_trade, short_trade = self._legs(result)
        self.assertAlmostEqual(long_trade.size, short_trade.size)
        midpoint = (long_trade.entry_price + short_trade.entry_price) / 2
        self.assertAlmostEqual(midpoint, PAIR_ENTRY_PRICE, places=6)
        self.assertGreater(long_trade.entry_price, short_trade.entry_price)

    def test_trend_signal_cuts_the_short_and_lets_the_long_run(self) -> None:
        # A wide initial stop guarantees the trigger fires before the ATR stop.
        settings = make_settings(STOP_ATR_MULTIPLIER="10")
        cfg = StraddleConfig(trigger="trend_signal")
        result = straddle_run(settings, cfg, RALLY_PATH)

        pair = result.pairs[0]
        self.assertEqual(pair.winner, "long")
        self.assertEqual(pair.cut_reason, CUT_BY_TRIGGER)

        loser = next(t for t in result.trades if t.side == "sell")
        winner = next(t for t in result.trades if t.side == "buy")
        self.assertEqual(loser.exit_reason, "cut_loser")
        self.assertEqual(winner.exit_reason, "trail")
        self.assertGreater(winner.closed_at, loser.closed_at)
        self.assertLess(pair.loser_pnl, 0)
        self.assertGreater(pair.winner_pnl, 0)

    def test_downtrend_cuts_the_long_instead(self) -> None:
        settings = make_settings(STOP_ATR_MULTIPLIER="10")
        cfg = StraddleConfig(trigger="trend_signal")
        result = straddle_run(settings, cfg, SLIDE_PATH)

        pair = result.pairs[0]
        self.assertEqual(pair.winner, "short")
        self.assertEqual(pair.cut_reason, CUT_BY_TRIGGER)
        loser = next(t for t in result.trades if t.side == "buy")
        self.assertEqual(loser.exit_reason, "cut_loser")
        self.assertLess(pair.loser_pnl, 0)
        self.assertGreater(pair.winner_pnl, 0)

    def test_atr_trigger_cuts_at_the_computed_level(self) -> None:
        cfg = StraddleConfig(trigger="atr", trigger_atr_mult=1.0)
        result = straddle_run(self.settings, cfg, RALLY_PATH)
        pair = result.pairs[0]

        # ATR over 35 flat bars with a 0.5 spread each side is exactly 1.0.
        window = series(RALLY_PATH)[:FLAT_BARS]
        self.assertAlmostEqual(atr_value(window, self.settings), 1.0)
        self.assertAlmostEqual(pair.cut_price, PAIR_ENTRY_PRICE + 1.0)

        loser = next(t for t in result.trades if t.side == "sell")
        self.assertAlmostEqual(loser.exit_price, PAIR_ENTRY_PRICE + 1.0)

    def test_a_leg_stopped_before_the_trigger_hands_the_pair_over(self) -> None:
        # A tight stop and a far-off trigger: the short is stopped, not cut.
        settings = make_settings(STOP_ATR_MULTIPLIER="1.0")
        cfg = StraddleConfig(trigger="atr", trigger_atr_mult=50.0)
        result = straddle_run(settings, cfg, RALLY_PATH)

        pair = result.pairs[0]
        self.assertEqual(pair.cut_reason, "stop_pre_trigger")
        self.assertEqual(pair.winner, "long")
        loser = next(t for t in result.trades if t.side == "sell")
        self.assertEqual(loser.exit_reason, "stop_pre_trigger")

    def _legs(self, result):
        long_trade = next(t for t in result.trades if t.side == "buy")
        short_trade = next(t for t in result.trades if t.side == "sell")
        return long_trade, short_trade


class EquivalenceTest(unittest.TestCase):
    """The straddle should equal a single entry at the cut price, minus fees.

    This is the algebra the whole idea rests on: a symmetric perp pair nets to
    zero until one side is closed, so cutting the loser at price C leaves you
    holding exactly what a single directional entry at C would have held, while
    having paid for an extra round trip.
    """

    def test_straddle_equals_single_entry_at_cut_price_minus_extra_fees(self) -> None:
        settings = make_settings(SLIPPAGE_BPS="0")
        cfg = StraddleConfig(trigger="atr", trigger_atr_mult=1.0)
        result = straddle_run(settings, cfg, RALLY_PATH)

        self.assertEqual(len(result.pairs), 1)
        pair = result.pairs[0]
        loser = next(t for t in result.trades if t.side == "sell")
        winner = next(t for t in result.trades if t.side == "buy")

        size = winner.size
        cut_price = pair.cut_price
        exit_price = winner.exit_price
        entry_rate = settings.entry_fee_bps / 10_000
        exit_rate = settings.exit_fee_bps / 10_000

        # BTTrade.pnl excludes entry fees (equity is charged at open), so add
        # them back to get the true equity impact of the cycle.
        entry_fees = 2 * pair.ref_price * size * entry_rate
        straddle_delta = pair.net_pnl - entry_fees

        single_delta = (
            (exit_price - cut_price) * size
            - cut_price * size * entry_rate
            - exit_price * size * exit_rate
        )
        extra_fee_drag = (
            cut_price * size * exit_rate
            + 2 * pair.ref_price * size * entry_rate
            - cut_price * size * entry_rate
        )

        self.assertAlmostEqual(straddle_delta, single_delta - extra_fee_drag, places=9)
        self.assertGreater(extra_fee_drag, 0)
        self.assertAlmostEqual(
            result.final_equity - result.starting_equity, straddle_delta, places=9
        )

    def test_equivalence_also_holds_for_a_short_winner(self) -> None:
        settings = make_settings(SLIPPAGE_BPS="0")
        cfg = StraddleConfig(trigger="atr", trigger_atr_mult=1.0)
        result = straddle_run(settings, cfg, SLIDE_PATH)

        self.assertEqual(len(result.pairs), 1)
        pair = result.pairs[0]
        self.assertEqual(pair.winner, "short")
        winner = next(t for t in result.trades if t.side == "sell")

        size = winner.size
        entry_rate = settings.entry_fee_bps / 10_000
        exit_rate = settings.exit_fee_bps / 10_000
        cut_price = pair.cut_price

        straddle_delta = pair.net_pnl - 2 * pair.ref_price * size * entry_rate
        single_delta = (
            (cut_price - winner.exit_price) * size
            - cut_price * size * entry_rate
            - winner.exit_price * size * exit_rate
        )
        extra_fee_drag = (
            cut_price * size * exit_rate
            + 2 * pair.ref_price * size * entry_rate
            - cut_price * size * entry_rate
        )
        self.assertAlmostEqual(straddle_delta, single_delta - extra_fee_drag, places=9)


class SettingsGuardTest(unittest.TestCase):
    def test_engine_rejects_a_series_shorter_than_the_lookback(self) -> None:
        settings = make_settings()
        cfg = StraddleConfig()
        with self.assertRaises(ValueError):
            straddle_run(settings, cfg, [100.0] * 10)

    def test_lookback_uses_the_larger_of_the_two_windows(self) -> None:
        settings = replace(make_settings(), lookback_candles=5, high_lookback_candles=40)
        cfg = StraddleConfig()
        with self.assertRaises(ValueError):
            straddle_run(settings, cfg, [100.0] * 20)


if __name__ == "__main__":
    unittest.main()
