"""Tests for STRATEGY=copy (``src.copy_trade``), all offline.

The planner is where a copy silently drifts from the leader: following a
ratio wobble instead of a real trade, holding a coin the leader doesn't, or a
pause that blocks an exit. Each of those is pinned below, then the paper
ledger's accounting, then the trader end to end against a fake leader.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.copy_trade import (
    AssetCtx,
    CopyPaperBroker,
    CopyTrader,
    LeaderBook,
    dex_of,
    plan_orders,
)
from tests.helpers import make_settings

PX = {"ETH": 2000.0, "BTC": 50_000.0, "xyz:NVDA": 200.0, "DOGE": 0.1}
DEC = {"ETH": 4, "BTC": 5, "xyz:NVDA": 3, "DOGE": 0}


def plan(leader, prev=None, mine=None, *, first=False, waiting=(), ratio=0.001, allow=True,
         max_notional=1e9, sync=True, min_notional=10.0):
    return plan_orders(
        leader=leader,
        prev_leader=prev or {},
        first_tick=first,
        mine=mine or {},
        waiting=set(waiting),
        ratio=ratio,
        price=PX.__getitem__,
        decimals=DEC.__getitem__,
        min_notional=min_notional,
        allow_increase=allow,
        max_notional=max_notional,
        sync_on_start=sync,
    )


class TestPlanOrders(unittest.TestCase):
    def test_first_tick_joins_open_positions(self):
        orders, skips, _ = plan({"ETH": 200.0}, first=True)
        self.assertEqual([(o.coin, o.delta, o.reason) for o in orders], [("ETH", 0.2, "open")])
        self.assertEqual(skips, [])

    def test_unchanged_leader_is_not_rebalanced(self):
        # Our ratio drifted (0.001 -> 0.0015) but the leader did nothing: no trade.
        orders, _, _ = plan({"ETH": 200.0}, prev={"ETH": 200.0}, mine={"ETH": 0.2}, ratio=0.0015)
        self.assertEqual(orders, [])

    def test_add_reduce_close(self):
        o, _, _ = plan({"ETH": 300.0}, prev={"ETH": 200.0}, mine={"ETH": 0.2})
        self.assertEqual((o[0].delta, o[0].reason), (0.1, "add"))
        o, _, _ = plan({"ETH": 100.0}, prev={"ETH": 200.0}, mine={"ETH": 0.2})
        self.assertEqual((o[0].delta, o[0].reason), (-0.1, "reduce"))
        o, _, _ = plan({}, prev={"ETH": 200.0}, mine={"ETH": 0.2})
        self.assertEqual((o[0].delta, o[0].target, o[0].reason), (-0.2, 0.0, "close"))

    def test_flip(self):
        o, _, _ = plan({"ETH": -100.0}, prev={"ETH": 200.0}, mine={"ETH": 0.2})
        self.assertEqual((o[0].delta, o[0].target, o[0].reason), (-0.3, -0.1, "flip"))

    def test_orphan_is_closed(self):
        o, _, _ = plan({}, prev={}, mine={"BTC": 0.01})
        self.assertEqual((o[0].coin, o[0].delta, o[0].reason), ("BTC", -0.01, "orphan"))

    def test_hip3_coin_is_followed_like_any_other(self):
        o, _, _ = plan({"xyz:NVDA": 500.0}, prev={})
        self.assertEqual((o[0].coin, o[0].delta), ("xyz:NVDA", 0.5))
        self.assertEqual(dex_of("xyz:NVDA"), "xyz")
        self.assertEqual(dex_of("ETH"), "")

    def test_below_minimum_is_skipped(self):
        # 2 ETH * 0.001 = 0.002 ETH = $4: under the $10 minimum.
        orders, skips, _ = plan({"ETH": 2.0}, prev={})
        self.assertEqual(orders, [])
        self.assertIn("below", skips[0].reason)

    def test_reduction_to_a_stub_closes_instead(self):
        # Leader cuts 200 -> 4: our 0.004 ETH ($8) would be under the minimum.
        o, _, _ = plan({"ETH": 4.0}, prev={"ETH": 200.0}, mine={"ETH": 0.2})
        self.assertEqual((o[0].delta, o[0].target, o[0].reason), (-0.2, 0.0, "close"))

    def test_pause_blocks_increases_but_never_exits(self):
        o, skips, _ = plan({"ETH": 300.0}, prev={"ETH": 200.0}, mine={"ETH": 0.2}, allow=False)
        self.assertEqual(o, [])
        self.assertIn("paused", skips[0].reason)
        o, _, _ = plan({"ETH": 100.0}, prev={"ETH": 200.0}, mine={"ETH": 0.2}, allow=False)
        self.assertEqual(o[0].reason, "reduce")
        # A flip while paused closes the old side and does not open the new one.
        o, _, _ = plan({"ETH": -100.0}, prev={"ETH": 200.0}, mine={"ETH": 0.2}, allow=False)
        self.assertEqual([(x.delta, x.reason) for x in o], [(-0.2, "close")])

    def test_max_leverage_caps_increases_only(self):
        # $1,000 allowed in total; we already hold BTC $500, so ETH may add $500 = 0.25 ETH.
        o, skips, _ = plan(
            {"ETH": 400.0, "BTC": 10.0}, prev={"BTC": 10.0}, mine={"BTC": 0.01}, max_notional=1000
        )
        self.assertEqual([(x.coin, x.delta) for x in o], [("ETH", 0.25)])
        self.assertIn("MAX_LEVERAGE", skips[0].reason)
        # Over the cap already: a reduction still goes through.
        o, _, _ = plan({"BTC": 5.0}, prev={"BTC": 10.0}, mine={"BTC": 0.01}, max_notional=1)
        self.assertEqual(o[0].reason, "reduce")

    def test_no_sync_on_start_waits_for_the_leader_to_go_flat(self):
        orders, _, waiting = plan({"ETH": 200.0}, first=True, sync=False)
        self.assertEqual((orders, waiting), ([], {"ETH"}))
        orders, _, waiting = plan({"ETH": 300.0}, prev={"ETH": 200.0}, waiting=waiting, sync=False)
        self.assertEqual(orders, [])  # still mid-trade: not ours to join
        orders, _, waiting = plan({}, prev={"ETH": 300.0}, waiting=waiting, sync=False)
        self.assertEqual((orders, waiting), ([], set()))
        orders, _, _ = plan({"ETH": 100.0}, prev={}, waiting=waiting, sync=False)
        self.assertEqual(orders[0].reason, "open")

    def test_size_rounds_to_asset_decimals(self):
        o, _, _ = plan({"DOGE": 123_456.0}, prev={}, ratio=0.001)
        self.assertEqual(o[0].delta, 123.0)


class TestPaperLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(LOGS_DIR=self.tmp.name, INITIAL_EQUITY=1000)
        self.broker = CopyPaperBroker(self.settings)

    def tearDown(self):
        self.tmp.cleanup()

    def trades(self):
        path = Path(self.tmp.name) / "trades.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_average_entry_partial_reduce_and_close(self):
        b = self.broker
        b.fill("ETH", 1.0, 100.0, 0, "open")
        b.fill("ETH", 1.0, 200.0, 0, "add")
        self.assertAlmostEqual(b.positions["ETH"].entry, 150.0)
        b.fill("ETH", -1.0, 170.0, 0, "reduce")  # +20 realized
        self.assertAlmostEqual(b.cash, 1020.0)
        self.assertAlmostEqual(b.positions["ETH"].entry, 150.0)
        closed = b.fill("ETH", -1.0, 140.0, 0, "close")  # -10 realized
        self.assertEqual(len(closed), 1)
        self.assertAlmostEqual(closed[0].pnl, 10.0)
        self.assertAlmostEqual(closed[0].exit_price, 155.0)
        self.assertEqual(closed[0].size, 2.0)
        self.assertNotIn("ETH", b.positions)
        self.assertEqual(self.trades()[0]["exit_reason"], "copy_close")

    def test_fees_split_across_a_flip(self):
        b = self.broker
        b.fill("ETH", 1.0, 100.0, 0, "open")
        closed = b.fill("ETH", -3.0, 110.0, 10, "flip")  # 3 * 110 * 10bps = 0.33 fee
        self.assertAlmostEqual(closed[0].fees, 0.11)
        self.assertAlmostEqual(closed[0].pnl, 10.0 - 0.11)
        pos = b.positions["ETH"]
        self.assertAlmostEqual(pos.size, -2.0)
        self.assertAlmostEqual(pos.entry, 110.0)
        self.assertAlmostEqual(pos.fees, 0.22)
        self.assertEqual(pos.side, "sell")
        self.assertAlmostEqual(b.cash, 1000 + 10 - 0.33)

    def test_funding_direction_and_marked_equity(self):
        b = self.broker
        b.fill("ETH", 2.0, 100.0, 0, "open")
        self.assertAlmostEqual(b.apply_funding("ETH", 0.001, 100.0), -0.2)  # long pays
        b.fill("BTC", -1.0, 100.0, 0, "open")
        self.assertAlmostEqual(b.apply_funding("BTC", 0.001, 100.0), 0.1)  # short receives
        self.assertAlmostEqual(b.mark({"ETH": 110.0, "BTC": 90.0}), 1000 - 0.2 + 0.1 + 20 + 10)
        closed = b.fill("ETH", -2.0, 100.0, 0, "close")
        self.assertAlmostEqual(closed[0].pnl, -0.2)  # funding counts toward the trade
        self.assertAlmostEqual(self.trades()[0]["funding"], -0.2)

    def test_state_round_trip(self):
        self.broker.fill("ETH", 1.5, 100.0, 4.5, "open")
        other = CopyPaperBroker(self.settings)
        other.load_state(json.loads(json.dumps(self.broker.to_state())))
        self.assertAlmostEqual(other.cash, self.broker.cash)
        self.assertEqual(other.positions["ETH"], self.broker.positions["ETH"])


class FakeClient:
    def __init__(self):
        self.book = LeaderBook(equity=1_000_000.0, positions={})
        self.px = dict(PX)
        self.funding = 0.0

    def new_tick(self):
        pass

    def leader(self, address):
        return LeaderBook(self.book.equity, dict(self.book.positions))

    def ctx(self, coin):
        return AssetCtx(mid=self.px[coin], oracle=self.px[coin], funding=self.funding, sz_decimals=DEC[coin])


class TestCopyTrader(unittest.TestCase):
    LEADER = "0x" + "ab" * 20

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(
            STRATEGY="copy", COPY_LEADER=self.LEADER, LOGS_DIR=self.tmp.name, INITIAL_EQUITY=10_000,
            COPY_TAKER_FEE_BPS=0, COPY_HIP3_FEE_BPS=0, SLIPPAGE_BPS=0, MAX_LEVERAGE=50,
        )
        self.client = FakeClient()

    def tearDown(self):
        self.tmp.cleanup()

    def trader(self):
        return CopyTrader(self.settings, CopyPaperBroker(self.settings), client=self.client)

    def test_follows_proportionally_and_survives_restart(self):
        t = self.trader()
        self.client.book.positions = {"ETH": 100.0, "xyz:NVDA": 1000.0}
        t.tick(paused=False)
        # ratio = 10k / 1M = 0.01
        self.assertAlmostEqual(t.broker.positions["ETH"].size, 1.0)
        self.assertAlmostEqual(t.broker.positions["xyz:NVDA"].size, 10.0)

        # Restart: state reloads, nothing is re-bought.
        t2 = self.trader()
        self.assertFalse(t2.first_tick)
        t2.tick(paused=False)
        self.assertAlmostEqual(t2.broker.positions["ETH"].size, 1.0)

        # The leader closes ETH at a profit while we were restarted.
        self.client.px["ETH"] = 2200.0
        self.client.book.positions = {"xyz:NVDA": 1000.0}
        t2.tick(paused=False)
        self.assertNotIn("ETH", t2.broker.positions)
        self.assertAlmostEqual(t2.broker.cash, 10_000 + 200.0)

    def test_new_leader_discards_old_state(self):
        t = self.trader()
        self.client.book.positions = {"ETH": 100.0}
        t.tick(paused=False)
        other = make_settings(STRATEGY="copy", COPY_LEADER="0x" + "cd" * 20, LOGS_DIR=self.tmp.name)
        t2 = CopyTrader(other, CopyPaperBroker(other), client=self.client)
        self.assertTrue(t2.first_tick)
        self.assertEqual(t2.broker.positions, {})

    def test_requires_leader(self):
        with self.assertRaises(ValueError):
            CopyTrader(make_settings(STRATEGY="copy", LOGS_DIR=self.tmp.name),
                       CopyPaperBroker(self.settings), client=self.client)


class TestBotGate(unittest.TestCase):
    def test_copy_refuses_live_mode(self):
        from src.bot import FuturesBot

        with tempfile.TemporaryDirectory() as tmp:
            s = make_settings(STRATEGY="copy", MODE="live", COPY_LEADER="0x" + "ab" * 20, LOGS_DIR=tmp)
            with self.assertRaises(RuntimeError):
                FuturesBot(s)


if __name__ == "__main__":
    unittest.main()
