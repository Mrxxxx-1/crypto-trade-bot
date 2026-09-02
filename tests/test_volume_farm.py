"""Tests for the volume farmer: sizing, accounting, and the collision guards.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.volume_farm import (  # noqa: E402
    MARGIN_SAFETY,
    FarmPlan,
    VolumeFarmer,
    check_collisions,
    plan_farm,
)
from tests.test_straddle import make_settings  # noqa: E402

FARM_SYMBOL = "SOL/USDC:USDC"
BOT_SYMBOL = "BTC/USDC:USDC"


class FakeAdapter:
    """Stands in for ExchangeAdapter: fills at mid, tracks orders."""

    def __init__(self, price: float = 100.0, equity: float = 1000.0, positions=None) -> None:
        self.price = price
        self._equity = equity
        self._positions = positions or []
        self.orders: list[dict] = []
        self.fail_next = 0
        self.slip = 0.0

    def fetch_last_price(self, symbol: str) -> float:
        return self.price

    def fetch_balance(self) -> float:
        return self._equity

    def fetch_positions(self):
        return self._positions

    def round_size(self, symbol: str, size: float) -> float:
        return round(size, 3)

    def create_market_order(self, symbol, side, amount, params=None):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("exchange rejected the order")
        params = params or {}
        self.orders.append(
            {"symbol": symbol, "side": side, "amount": amount, "reduce": bool(params.get("reduceOnly"))}
        )
        fill = self.price + (self.slip if side == "buy" else -self.slip)
        return {"average": fill, "filled": amount, "status": "closed", "id": "1"}


def make_plan(**overrides) -> FarmPlan:
    base = dict(
        symbol=FARM_SYMBOL,
        target_volume=10_000.0,
        price=100.0,
        equity=1000.0,
        leverage=5.0,
        notional_per_trip=4_000.0,
        size_per_trip=40.0,
        trips=2,
        taker_rate=0.00045,
    )
    base.update(overrides)
    return FarmPlan(**base)


class PlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings()
        self.adapter = FakeAdapter(price=100.0, equity=1000.0)

    def test_notional_respects_leverage_and_margin_headroom(self) -> None:
        plan = plan_farm(self.settings, self.adapter, FARM_SYMBOL, 10_000, leverage=5.0)
        self.assertAlmostEqual(plan.notional_per_trip, 1000.0 * 5.0 * MARGIN_SAFETY, places=2)
        self.assertLess(plan.notional_per_trip, 1000.0 * 5.0)

    def test_a_round_trip_counts_double_its_notional(self) -> None:
        plan = make_plan(notional_per_trip=4_000.0)
        self.assertAlmostEqual(plan.volume_per_trip, 8_000.0)

    def test_trip_count_covers_the_target(self) -> None:
        plan = plan_farm(self.settings, self.adapter, FARM_SYMBOL, 40_000, leverage=5.0)
        self.assertGreaterEqual(plan.trips * plan.volume_per_trip, 40_000)

    def test_fees_are_proportional_to_volume_not_trip_count(self) -> None:
        """Bigger trips finish faster but cost exactly the same."""
        small = plan_farm(self.settings, self.adapter, FARM_SYMBOL, 50_000, leverage=2.0)
        large = plan_farm(self.settings, self.adapter, FARM_SYMBOL, 50_000, leverage=20.0)
        self.assertGreater(small.trips, large.trips)
        self.assertAlmostEqual(small.estimated_fees, large.estimated_fees, places=6)

    def test_max_notional_caps_the_trip(self) -> None:
        plan = plan_farm(
            self.settings, self.adapter, FARM_SYMBOL, 10_000, leverage=20.0, max_notional=500.0
        )
        self.assertLessEqual(plan.notional_per_trip, 500.0)

    def test_rejects_a_target_or_leverage_of_zero(self) -> None:
        with self.assertRaises(ValueError):
            plan_farm(self.settings, self.adapter, FARM_SYMBOL, 0, leverage=5.0)
        with self.assertRaises(ValueError):
            plan_farm(self.settings, self.adapter, FARM_SYMBOL, 1000, leverage=0)

    def test_rejects_an_empty_account(self) -> None:
        with self.assertRaises(ValueError):
            plan_farm(self.settings, FakeAdapter(equity=0.0), FARM_SYMBOL, 1000, 5.0)

    def test_rejects_a_coin_whose_size_rounds_to_zero(self) -> None:
        # A very expensive coin against a tiny account.
        adapter = FakeAdapter(price=1_000_000.0, equity=1.0)
        with self.assertRaises(ValueError):
            plan_farm(self.settings, adapter, FARM_SYMBOL, 1000, leverage=1.0)


class CollisionGuardTest(unittest.TestCase):
    """The critical guard: never churn a coin the trading bot is holding."""

    def setUp(self) -> None:
        self.settings = replace(make_settings(), symbols=[BOT_SYMBOL])

    def test_refuses_a_coin_the_bot_trades(self) -> None:
        problems = check_collisions(self.settings, FakeAdapter(), BOT_SYMBOL, force=False)
        self.assertTrue(problems)
        self.assertIn("SYMBOLS", problems[0])

    def test_allows_a_bot_coin_only_when_forced(self) -> None:
        self.assertFalse(check_collisions(self.settings, FakeAdapter(), BOT_SYMBOL, force=True))

    def test_allows_an_unrelated_coin(self) -> None:
        self.assertFalse(check_collisions(self.settings, FakeAdapter(), FARM_SYMBOL, force=False))

    def test_refuses_when_the_target_coin_already_has_a_position(self) -> None:
        adapter = FakeAdapter(positions=[{"coin": "SOL", "side": "buy", "size": 1.0}])
        problems = check_collisions(self.settings, adapter, FARM_SYMBOL, force=False)
        self.assertTrue(problems)
        self.assertIn("already has an open", problems[0])

    def test_an_open_position_blocks_even_with_force(self) -> None:
        adapter = FakeAdapter(positions=[{"coin": "SOL", "side": "sell", "size": 2.0}])
        self.assertTrue(check_collisions(self.settings, adapter, FARM_SYMBOL, force=True))

    def test_a_position_on_another_coin_is_irrelevant(self) -> None:
        adapter = FakeAdapter(positions=[{"coin": "ETH", "side": "buy", "size": 1.0}])
        self.assertFalse(check_collisions(self.settings, adapter, FARM_SYMBOL, force=False))


class ExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings()
        self.adapter = FakeAdapter(price=100.0, equity=1000.0)
        self.farmer = VolumeFarmer(
            self.settings, self.adapter, pause=0.0, sleep=lambda _s: None, log=lambda _m: None
        )

    def test_each_trip_opens_then_closes_reduce_only(self) -> None:
        self.farmer.run(make_plan(target_volume=8_000.0))
        self.assertEqual(len(self.adapter.orders), 2)
        self.assertEqual(self.adapter.orders[0]["side"], "buy")
        self.assertFalse(self.adapter.orders[0]["reduce"])
        self.assertEqual(self.adapter.orders[1]["side"], "sell")
        self.assertTrue(self.adapter.orders[1]["reduce"])

    def test_it_closes_exactly_what_filled(self) -> None:
        self.farmer.run(make_plan(target_volume=8_000.0))
        self.assertAlmostEqual(
            self.adapter.orders[0]["amount"], self.adapter.orders[1]["amount"]
        )

    def test_volume_accumulates_until_the_target_is_met(self) -> None:
        result = self.farmer.run(make_plan(target_volume=20_000.0))
        self.assertGreaterEqual(result.volume, 20_000.0)
        self.assertEqual(result.trips, 3)  # 8k per trip
        self.assertEqual(len(self.adapter.orders), 6)

    def test_fees_track_the_generated_volume(self) -> None:
        plan = make_plan(target_volume=8_000.0)
        result = self.farmer.run(plan)
        self.assertAlmostEqual(result.fees, result.volume * plan.taker_rate, places=6)

    def test_a_frictionless_round_trip_has_no_pnl(self) -> None:
        result = self.farmer.run(make_plan(target_volume=8_000.0))
        self.assertAlmostEqual(result.realized_pnl, 0.0, places=6)
        self.assertTrue(result.ok)

    def test_slippage_shows_up_as_a_loss(self) -> None:
        self.adapter.slip = 0.5
        result = self.farmer.run(make_plan(target_volume=8_000.0))
        self.assertLess(result.realized_pnl, 0.0)

    def test_a_transient_failure_is_retried(self) -> None:
        self.adapter.fail_next = 1
        result = self.farmer.run(make_plan(target_volume=8_000.0))
        self.assertTrue(result.ok)
        self.assertEqual(result.trips, 1)

    def test_three_consecutive_failures_abort_the_run(self) -> None:
        self.adapter.fail_next = 99
        result = self.farmer.run(make_plan(target_volume=8_000.0))
        self.assertFalse(result.ok)
        self.assertIn("three consecutive failures", result.aborted)

    def test_an_equity_collapse_aborts_the_run(self) -> None:
        plan = make_plan(target_volume=1_000_000.0, equity=1000.0)
        self.adapter._equity = 500.0  # already below the 10% floor
        result = self.farmer.run(plan)
        self.assertIn("below the", result.aborted)
        self.assertEqual(result.trips, 1)

    def test_the_trip_cap_stops_a_runaway_loop(self) -> None:
        # A plan whose per-trip volume cannot reach the target.
        plan = make_plan(target_volume=1_000_000.0, trips=1)
        result = self.farmer.run(plan)
        self.assertIn("trip cap", result.aborted)


if __name__ == "__main__":
    unittest.main()
