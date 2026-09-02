"""Tests for the manual catalyst hedge: state machine, capped loss, and safety.

The executors are fakes, so the whole lifecycle is exercised without a network.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import agent_tools, hedge  # noqa: E402
from src.hedge import CLOSED, CUT, EXPIRED, FAILED, LONG, OPEN, REQUESTED, SHORT  # noqa: E402
from src.hedge_broker import MAIN, SUB, HedgeManager  # noqa: E402
from src.telegram_control import _handle_hedge  # noqa: E402
from tests.test_straddle import make_settings  # noqa: E402

SYMBOL = "BTC/USDC:USDC"


def flat_candles(n: int = 200, price: float = 100.0, spread: float = 0.5) -> list[list]:
    """Bars with a true range of exactly ``spread * 2``, so ATR is predictable."""
    return [
        [i * 3600_000, price, price + spread, price - spread, price, 100.0]
        for i in range(n)
    ]


class FakeExecutor:
    """Records orders and fills them at the requested reference price."""

    def __init__(self, account: str, equity: float = 500.0, fail_on_open: bool = False) -> None:
        self.account = account
        self._equity = equity
        self.fail_on_open = fail_on_open
        self.opens: list[tuple] = []
        self.closes: list[tuple] = []
        self.slip = 0.0

    def equity(self) -> float:
        return self._equity

    def round_size(self, symbol: str, size: float) -> float:
        return round(size, 5)

    def open(self, symbol: str, side: str, size: float, ref_price: float):
        if self.fail_on_open:
            raise RuntimeError(f"{self.account} rejected the order")
        self.opens.append((symbol, side, size, ref_price))
        return ref_price + self.slip, size

    def close(self, symbol: str, size: float, ref_price: float):
        self.closes.append((symbol, size, ref_price))
        return ref_price + self.slip, size


class HedgeTestCase(unittest.TestCase):
    """Shared fixture: a configured hedge over a temp logs dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = replace(
            make_settings(),
            logs_dir=self._tmp.name,
            hedge_enabled=True,
            hedge_sub_account="0x" + "ab" * 20,
            hedge_symbols=[SYMBOL],
            hedge_risk_pct=1.0,
            hedge_stop_atr_mult=2.0,
            hedge_trail_atr_mult=4.0,
            hedge_atr_floor_pct=0.0,
            max_leverage=3.0,
            entry_fee_bps=0.0,
            exit_fee_bps=0.0,
        )
        self.main = FakeExecutor(MAIN, equity=500.0)
        self.sub = FakeExecutor(SUB, equity=500.0)
        self.events: list[tuple[str, dict]] = []
        self.price = 100.0

    def manager(self, **overrides) -> HedgeManager:
        settings = replace(self.settings, **overrides) if overrides else self.settings
        return HedgeManager(
            settings,
            main=self.main,
            sub=self.sub,
            log_event=lambda e, p: self.events.append((e, p)),
        )

    def poll(self, mgr: HedgeManager, price: float | None = None):
        if price is not None:
            self.price = price
        return mgr.poll(lambda _s: flat_candles(), lambda _s: self.price)

    def arm(self, mgr: HedgeManager, note: str = "CPI") -> None:
        result = hedge.request_hedge(self.settings, "BTC", note=note)
        self.assertTrue(result["ok"], result)
        self.poll(mgr, 100.0)


class OpenPairTest(HedgeTestCase):
    def test_arming_writes_a_request_the_bot_picks_up(self) -> None:
        result = hedge.request_hedge(self.settings, "BTC", note="FOMC")
        self.assertTrue(result["ok"])
        state = hedge.read_hedge(self.settings.logs_dir)
        self.assertEqual(state.state, REQUESTED)
        self.assertEqual(state.symbol, SYMBOL)
        self.assertEqual(state.note, "FOMC")

    def test_both_legs_open_at_equal_size_in_separate_accounts(self) -> None:
        mgr = self.manager()
        self.arm(mgr)

        state = hedge.read_hedge(self.settings.logs_dir)
        self.assertEqual(state.state, OPEN)
        long_leg, short_leg = state.leg(LONG), state.leg(SHORT)
        self.assertEqual(long_leg.account, MAIN)
        self.assertEqual(short_leg.account, SUB)
        self.assertAlmostEqual(long_leg.size, short_leg.size)
        self.assertAlmostEqual(long_leg.entry_price, short_leg.entry_price)
        self.assertEqual(len(self.main.opens), 1)
        self.assertEqual(len(self.sub.opens), 1)
        self.assertEqual(self.main.opens[0][1], "buy")
        self.assertEqual(self.sub.opens[0][1], "sell")

    def test_stops_straddle_the_entry_so_only_one_can_trigger(self) -> None:
        mgr = self.manager()
        self.arm(mgr)
        state = hedge.read_hedge(self.settings.logs_dir)
        # ATR is 1.0 on the flat series; stop mult 2.0 -> 2.0 away from entry.
        self.assertAlmostEqual(state.ref_atr, 1.0)
        self.assertAlmostEqual(state.leg(LONG).stop_price, 98.0)
        self.assertAlmostEqual(state.leg(SHORT).stop_price, 102.0)
        self.assertLess(state.leg(LONG).stop_price, state.leg(SHORT).stop_price)

    def test_size_risks_the_configured_fraction_of_combined_equity(self) -> None:
        mgr = self.manager()
        self.arm(mgr)
        state = hedge.read_hedge(self.settings.logs_dir)
        # 1% of $1000 combined, over a 2.0 stop distance.
        self.assertAlmostEqual(state.leg(LONG).size, 10.0 / 2.0, places=5)

    def test_size_is_capped_by_the_smaller_account(self) -> None:
        self.sub._equity = 10.0
        mgr = self.manager(hedge_risk_pct=50.0)
        self.arm(mgr)
        state = hedge.read_hedge(self.settings.logs_dir)
        cap = (10.0 * self.settings.max_leverage) / 100.0
        self.assertLessEqual(state.leg(LONG).size, cap + 1e-9)

    def test_a_failed_second_leg_unwinds_the_first(self) -> None:
        self.sub.fail_on_open = True
        mgr = self.manager()
        self.arm(mgr)

        state = hedge.read_hedge(self.settings.logs_dir)
        self.assertEqual(state.state, FAILED)
        self.assertIn("failed to open", state.error)
        # The long that did fill must not be left as a naked position.
        self.assertEqual(len(self.main.closes), 1)
        self.assertEqual(state.leg(LONG).exit_reason, "unwind_failed_open")
        self.assertFalse(state.leg(LONG).is_open)

    def test_a_stale_request_expires_instead_of_opening(self) -> None:
        hedge.request_hedge(self.settings, "BTC")
        state = hedge.read_hedge(self.settings.logs_dir)
        state.requested_at = "2020-01-01T00:00:00+00:00"
        hedge.write_hedge(self.settings.logs_dir, state)

        mgr = self.manager()
        self.poll(mgr, 100.0)
        self.assertEqual(hedge.read_hedge(self.settings.logs_dir).state, EXPIRED)
        self.assertEqual(self.main.opens, [])


class CutTest(HedgeTestCase):
    def test_a_move_up_cuts_the_short_for_a_capped_loss(self) -> None:
        mgr = self.manager()
        self.arm(mgr)
        size = hedge.read_hedge(self.settings.logs_dir).leg(SHORT).size

        self.poll(mgr, 103.0)  # through the short stop at 102
        state = hedge.read_hedge(self.settings.logs_dir)

        self.assertEqual(state.state, CUT)
        self.assertEqual(state.winner, LONG)
        self.assertEqual(state.leg(SHORT).exit_reason, "cut_loser")
        self.assertFalse(state.leg(SHORT).is_open)
        self.assertTrue(state.leg(LONG).is_open)
        # Loss is bounded by the stop distance, not by how far price ran.
        self.assertAlmostEqual(state.leg(SHORT).realized_pnl, -2.0 * size, places=6)

    def test_a_move_down_cuts_the_long_instead(self) -> None:
        mgr = self.manager()
        self.arm(mgr)
        self.poll(mgr, 97.0)

        state = hedge.read_hedge(self.settings.logs_dir)
        self.assertEqual(state.winner, SHORT)
        self.assertEqual(state.leg(LONG).exit_reason, "cut_loser")
        self.assertLess(state.leg(LONG).realized_pnl, 0)

    def test_the_capped_loss_equals_the_configured_risk(self) -> None:
        mgr = self.manager()
        self.arm(mgr)
        self.poll(mgr, 103.0)
        state = hedge.read_hedge(self.settings.logs_dir)
        combined = self.main.equity() + self.sub.equity()
        expected = combined * (self.settings.hedge_risk_pct / 100.0)
        self.assertAlmostEqual(abs(state.leg(SHORT).realized_pnl), expected, places=6)

    def test_no_cut_while_price_stays_between_the_stops(self) -> None:
        mgr = self.manager()
        self.arm(mgr)
        for price in (100.5, 101.5, 99.0, 98.5):
            self.poll(mgr, price)
            self.assertEqual(hedge.read_hedge(self.settings.logs_dir).state, OPEN)


class WinnerTest(HedgeTestCase):
    def test_the_winner_trails_and_exits_on_the_trail(self) -> None:
        mgr = self.manager()
        self.arm(mgr)
        self.poll(mgr, 103.0)  # cut the short, long survives

        self.poll(mgr, 120.0)  # peak 120 -> trail at 120 - 4*1.0 = 116
        state = hedge.read_hedge(self.settings.logs_dir)
        self.assertEqual(state.state, CUT)
        self.assertAlmostEqual(state.leg(LONG).stop_price, 116.0)

        self.poll(mgr, 115.0)  # through the trail
        state = hedge.read_hedge(self.settings.logs_dir)
        self.assertEqual(state.state, CLOSED)
        self.assertEqual(state.leg(LONG).exit_reason, "trail")
        self.assertGreater(state.leg(LONG).realized_pnl, 0)

    def test_the_trail_ratchets_and_never_loosens(self) -> None:
        mgr = self.manager()
        self.arm(mgr)
        self.poll(mgr, 103.0)
        self.poll(mgr, 120.0)
        tight = hedge.read_hedge(self.settings.logs_dir).leg(LONG).stop_price
        self.poll(mgr, 118.0)  # a pullback must not widen the stop
        self.assertAlmostEqual(
            hedge.read_hedge(self.settings.logs_dir).leg(LONG).stop_price, tight
        )

    def test_a_short_winner_trails_downward(self) -> None:
        mgr = self.manager()
        self.arm(mgr)
        self.poll(mgr, 97.0)  # cut the long
        self.poll(mgr, 80.0)  # peak 80 -> trail at 84
        state = hedge.read_hedge(self.settings.logs_dir)
        self.assertAlmostEqual(state.leg(SHORT).stop_price, 84.0)
        self.poll(mgr, 85.0)
        state = hedge.read_hedge(self.settings.logs_dir)
        self.assertEqual(state.state, CLOSED)
        self.assertGreater(state.leg(SHORT).realized_pnl, 0)

    def test_hedge_pnl_nets_to_zero_at_the_cut_price(self) -> None:
        """The identity that makes a hedge risk-neutral, not profitable.

        With zero fees and no slippage, cutting the loser at price C leaves the
        pair's combined P&L equal to a single position entered at C.
        """
        mgr = self.manager()
        self.arm(mgr)
        self.poll(mgr, 103.0)
        state = hedge.read_hedge(self.settings.logs_dir)

        size = state.leg(LONG).size
        cut = state.cut_price
        loser_pnl = state.leg(SHORT).realized_pnl
        long_unrealized = state.leg(LONG).unrealized(cut)
        self.assertAlmostEqual(loser_pnl + long_unrealized, 0.0, places=6)
        self.assertAlmostEqual(long_unrealized, (cut - state.ref_price) * size, places=6)


class ManualCloseTest(HedgeTestCase):
    def test_disarming_a_request_never_opens_anything(self) -> None:
        hedge.request_hedge(self.settings, "BTC")
        result = hedge.request_close(self.settings)
        self.assertTrue(result["ok"])
        self.assertEqual(hedge.read_hedge(self.settings.logs_dir).state, EXPIRED)

        mgr = self.manager()
        self.poll(mgr, 100.0)
        self.assertEqual(self.main.opens, [])

    def test_closing_an_open_hedge_flattens_both_legs(self) -> None:
        mgr = self.manager()
        self.arm(mgr)
        hedge.request_close(self.settings)
        self.poll(mgr, 101.0)

        state = hedge.read_hedge(self.settings.logs_dir)
        self.assertEqual(state.state, CLOSED)
        self.assertFalse(state.open_legs)
        self.assertEqual(len(self.main.closes), 1)
        self.assertEqual(len(self.sub.closes), 1)

    def test_an_untriggered_hedge_closes_after_max_hours(self) -> None:
        mgr = self.manager()
        self.arm(mgr)
        state = hedge.read_hedge(self.settings.logs_dir)
        state.opened_at = "2020-01-01T00:00:00+00:00"
        hedge.write_hedge(self.settings.logs_dir, state)

        self.poll(mgr, 100.5)
        state = hedge.read_hedge(self.settings.logs_dir)
        self.assertEqual(state.state, CLOSED)
        self.assertEqual(state.leg(LONG).exit_reason, "hedge_max_hours")

    def test_only_one_hedge_may_be_active(self) -> None:
        hedge.request_hedge(self.settings, "BTC")
        second = hedge.request_hedge(self.settings, "BTC")
        self.assertFalse(second["ok"])
        self.assertIn("already", second["error"])

    def test_a_new_hedge_is_allowed_after_the_previous_one_closes(self) -> None:
        mgr = self.manager()
        self.arm(mgr)
        hedge.request_close(self.settings)
        self.poll(mgr, 100.0)
        self.assertTrue(hedge.request_hedge(self.settings, "BTC")["ok"])


class ValidationTest(HedgeTestCase):
    def test_unknown_symbols_are_rejected(self) -> None:
        result = hedge.request_hedge(self.settings, "DOGE")
        self.assertFalse(result["ok"])
        self.assertIn("HEDGE_SYMBOLS", result["error"])

    def test_symbols_resolve_from_the_bare_base(self) -> None:
        self.assertEqual(hedge.resolve_symbol(self.settings, "btc"), SYMBOL)
        self.assertEqual(hedge.resolve_symbol(self.settings, SYMBOL), SYMBOL)
        self.assertIsNone(hedge.resolve_symbol(self.settings, "sol"))

    def test_arming_requires_the_feature_and_a_sub_account(self) -> None:
        off = replace(self.settings, hedge_enabled=False)
        self.assertFalse(hedge.request_hedge(off, "BTC")["ok"])

        no_sub = replace(self.settings, hedge_sub_account="")
        self.assertFalse(hedge.request_hedge(no_sub, "BTC")["ok"])

    def test_poll_is_inert_when_the_feature_is_off(self) -> None:
        hedge.request_hedge(self.settings, "BTC")
        mgr = self.manager(hedge_enabled=False)
        self.assertIsNone(self.poll(mgr, 100.0))
        self.assertEqual(self.main.opens, [])

    def test_a_zero_atr_series_fails_instead_of_sizing_blindly(self) -> None:
        hedge.request_hedge(self.settings, "BTC")
        mgr = self.manager()
        mgr.poll(lambda _s: [], lambda _s: 100.0)
        state = hedge.read_hedge(self.settings.logs_dir)
        self.assertEqual(state.state, FAILED)
        self.assertEqual(self.main.opens, [])


class ReferenceAtrTest(unittest.TestCase):
    def test_floor_lifts_a_compressed_atr_to_the_percentile(self) -> None:
        # Long calm history, then a burst, then compression at the end.
        wide = flat_candles(150, spread=5.0)
        tight = flat_candles(20, spread=0.05)
        rows = wide + tight
        raw = hedge.reference_atr(rows, atr_period=14, floor_percentile=0.0)
        floored = hedge.reference_atr(rows, atr_period=14, floor_percentile=50.0)
        self.assertGreater(floored, raw)

    def test_floor_never_lowers_an_expanded_atr(self) -> None:
        rows = flat_candles(150, spread=0.1) + flat_candles(20, spread=9.0)
        raw = hedge.reference_atr(rows, atr_period=14, floor_percentile=0.0)
        floored = hedge.reference_atr(rows, atr_period=14, floor_percentile=50.0)
        self.assertAlmostEqual(floored, raw)

    def test_no_data_yields_zero(self) -> None:
        self.assertEqual(hedge.reference_atr([], 14, 50.0), 0.0)

    def test_wider_stops_come_from_a_floored_atr(self) -> None:
        """The concrete point of the floor: a bigger stop before a catalyst."""
        rows = flat_candles(150, spread=5.0) + flat_candles(20, spread=0.05)
        raw = hedge.reference_atr(rows, 14, 0.0)
        floored = hedge.reference_atr(rows, 14, 50.0)
        self.assertGreater(floored * 2.0, raw * 2.0)


class SafetyInvariantTest(unittest.TestCase):
    """The hedge must not become reachable by the language-model router."""

    def test_the_agent_tool_registry_exposes_no_hedge_tool(self) -> None:
        names = set(agent_tools.TOOL_SPECS)
        self.assertFalse(
            [n for n in names if "hedge" in n.lower()],
            "hedge must not be callable from the Gemini router",
        )

    def test_control_tools_remain_pause_and_resume_only(self) -> None:
        self.assertEqual(agent_tools.CONTROL_TOOLS, {"pause_trading", "resume_trading"})

    def test_the_tool_catalog_never_mentions_a_hedge_action(self) -> None:
        self.assertNotIn("hedge", agent_tools.tools_catalog_text().lower())


class TelegramCommandTest(HedgeTestCase):
    def test_status_reports_when_nothing_is_armed(self) -> None:
        self.assertIn("No hedge", _handle_hedge(self.settings, ["status"]))

    def test_bare_hedge_command_defaults_to_status(self) -> None:
        self.assertIn("No hedge", _handle_hedge(self.settings, []))

    def test_arm_without_a_symbol_shows_usage(self) -> None:
        reply = _handle_hedge(self.settings, ["arm"])
        self.assertIn("Usage", reply)
        self.assertIn("BTC", reply)

    def test_arm_then_status_then_close(self) -> None:
        armed = _handle_hedge(self.settings, ["arm", "BTC", "CPI", "print"])
        self.assertIn("arm", armed.lower())
        self.assertIn("CPI print", armed)

        self.assertIn(REQUESTED, _handle_hedge(self.settings, ["status"]))
        self.assertIn("disarm", _handle_hedge(self.settings, ["close"]).lower())

    def test_arm_rejects_an_unknown_symbol_with_a_reason(self) -> None:
        reply = _handle_hedge(self.settings, ["arm", "DOGE"])
        self.assertIn("rejected", reply.lower())

    def test_unknown_subcommand_shows_usage(self) -> None:
        self.assertIn("Usage", _handle_hedge(self.settings, ["yolo"]))


if __name__ == "__main__":
    unittest.main()
