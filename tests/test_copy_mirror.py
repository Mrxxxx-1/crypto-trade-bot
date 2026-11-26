"""Tests for COPY_MODE=mirror (``src.copy_mirror``), offline.

The claim this mode makes is exactness: mirror P&L == leader trading P&L x
ratio. ``test_pnl_is_exactly_the_leaders_scaled`` checks it against a hand-
computed sequence with adds, a partial close, a flip and fees. The rest pin the
plumbing that keeps it exact in practice: de-duplication at page edges,
self-healing from ``startPosition``, funding, and restarts.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.copy_mirror import MirrorTracker
from src.copy_trade import AssetCtx, CopyPaperBroker, LeaderBook
from tests.helpers import make_settings

T0 = 1_800_000_000_000


def fill(t, coin, side, sz, px, start, fee=0.0, closed=0.0, tid=None):
    return {
        "coin": coin, "side": side, "sz": str(sz), "px": str(px), "startPosition": str(start),
        "time": T0 + t, "tid": tid if tid is not None else t, "oid": t, "fee": str(fee),
        "closedPnl": str(closed), "dir": "x",
    }


class FakeInfo:
    WEIGHTS: dict = {}

    def __init__(self):
        self.fills: list[dict] = []
        self.funding: list[dict] = []

    def post(self, body):
        if body["type"] == "userFillsByTime":
            return [f for f in self.fills if f["time"] >= body["startTime"]]
        if body["type"] == "userFunding":
            return [e for e in self.funding if e["time"] >= body["startTime"]]
        raise AssertionError(body)


class FakeClient:
    def __init__(self):
        self.info = FakeInfo()
        self.book = LeaderBook(equity=1_000_000.0, positions={}, details={})

    def new_tick(self):
        pass

    def leader(self, address):
        return LeaderBook(self.book.equity, dict(self.book.positions), dict(self.book.details))

    def ctx(self, coin):
        return AssetCtx(mid=100.0, oracle=100.0, funding=0.0, sz_decimals=4)


class MirrorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(
            STRATEGY="copy", COPY_MODE="mirror", COPY_LEADER="0x" + "ab" * 20,
            LOGS_DIR=self.tmp.name, INITIAL_EQUITY=1000,
        )
        self.client = FakeClient()

    def tearDown(self):
        self.tmp.cleanup()

    def tracker(self):
        return MirrorTracker(self.settings, CopyPaperBroker(self.settings), client=self.client)

    def events(self, name):
        path = Path(self.tmp.name) / "events.jsonl"
        return [e for e in map(json.loads, path.read_text().splitlines()) if e["event"] == name]


class TestExactness(MirrorBase):
    def test_bootstrap_ratio_and_join_at_mark(self):
        self.client.book.positions = {"ETH": 200.0}
        self.client.book.details = {"ETH": {"value": 400_000.0}}  # mark 2000
        m = self.tracker()
        m.tick(paused=False)
        self.assertAlmostEqual(m.ratio, 0.001)  # $1k / $1M
        pos = m.broker.positions["ETH"]
        self.assertAlmostEqual(pos.size, 0.2)
        self.assertAlmostEqual(pos.entry, 2000.0)
        self.assertAlmostEqual(m.broker.cash, 1000.0)  # joining costs nothing

    def test_pnl_is_exactly_the_leaders_scaled(self):
        m = self.tracker()
        m.tick(paused=False)  # bootstrap flat
        # Leader: buy 10@100, buy 10@110 (avg 105), sell 5@120 (+75),
        # sell 25@90 = close 15 (-225) + open short 10, buy 10@80 (+100).
        self.client.info.fills = [
            fill(10, "BTC", "B", 10, 100, 0, fee=0.5),
            fill(11, "BTC", "B", 10, 110, 10, fee=0.55),
            fill(12, "BTC", "A", 5, 120, 20, fee=0.3, closed=75),
            fill(13, "BTC", "A", 25, 90, 15, fee=1.125, closed=-225),
            fill(14, "BTC", "B", 10, 80, -10, fee=0.4, closed=100),
        ]
        m.tick(paused=False)
        leader_pnl = (75 - 225 + 100) - (0.5 + 0.55 + 0.3 + 1.125 + 0.4)
        self.assertEqual(m.broker.positions, {})
        self.assertAlmostEqual(m.broker.cash - 1000.0, leader_pnl * m.ratio, places=12)
        self.assertEqual(m.fills_mirrored, 5)
        self.assertEqual(m.resyncs, 0)

    def test_tiny_fills_are_mirrored_with_no_minimum(self):
        m = self.tracker()
        m.tick(paused=False)
        self.client.info.fills = [fill(10, "xyz:NVDA", "B", 0.01, 200, 0)]  # $2 leader fill -> $0.002 for us
        m.tick(paused=False)
        self.assertAlmostEqual(m.broker.positions["xyz:NVDA"].size, 0.00001)

    def test_spot_fills_are_ignored(self):
        m = self.tracker()
        m.tick(paused=False)
        self.client.info.fills = [fill(10, "@107", "B", 5, 1, 0), fill(11, "PURR/USDC", "B", 5, 1, 0)]
        m.tick(paused=False)
        self.assertEqual(m.broker.positions, {})


class TestPlumbing(MirrorBase):
    def test_boundary_fills_are_not_double_counted(self):
        m = self.tracker()
        m.tick(paused=False)
        self.client.info.fills = [fill(10, "ETH", "B", 1, 100, 0)]
        m.tick(paused=False)
        m.tick(paused=False)  # startTime is inclusive: the same fill comes back
        self.assertAlmostEqual(m.broker.positions["ETH"].size, 0.001)
        self.assertEqual(m.fills_mirrored, 1)

    def test_same_millisecond_fills_keep_api_order(self):
        # tids out of order within one ms: sorting by tid would break the chain.
        m = self.tracker()
        m.tick(paused=False)
        self.client.info.fills = [
            fill(10, "ETH", "B", 3, 100, 0, tid=900),
            fill(10, "ETH", "B", 2, 100, 3, tid=100),
        ]
        m.tick(paused=False)
        self.assertEqual(m.resyncs, 0)
        self.assertAlmostEqual(m.broker.positions["ETH"].size, 0.005)

    def test_missed_fill_is_healed_from_start_position(self):
        m = self.tracker()
        m.tick(paused=False)
        # We never saw the leader open 5 ETH; the next fill says it started at 5.
        self.client.info.fills = [fill(10, "ETH", "B", 2, 100, 5)]
        m.tick(paused=False)
        self.assertEqual(m.resyncs, 1)
        self.assertAlmostEqual(m.broker.positions["ETH"].size, 0.007)
        self.assertEqual(self.events("mirror_resync")[0]["leader"], 5.0)

    def test_book_mismatch_resyncs_only_if_it_persists(self):
        m = self.tracker()
        m.tick(paused=False)
        self.client.book.positions = {"ETH": 10.0}
        self.client.book.details = {"ETH": {"value": 1000.0}}
        for _ in range(12 * 2):  # two book checks: still within the grace period
            m.tick(paused=False)
        self.assertNotIn("ETH", m.broker.positions)
        for _ in range(12):  # third consecutive mismatch
            m.tick(paused=False)
        self.assertAlmostEqual(m.broker.positions["ETH"].size, 0.01)

    def test_funding_is_the_leaders_scaled(self):
        m = self.tracker()
        m.tick(paused=False)
        self.client.info.fills = [fill(10, "ETH", "B", 100, 100, 0)]
        self.client.info.funding = [
            {"time": T0 + 3_600_000 * 10**6, "delta": {"coin": "ETH", "usdc": "-4.0", "szi": "100"}}
        ]
        for _ in range(12):
            m.tick(paused=False)
        self.assertAlmostEqual(m.broker.positions["ETH"].funding, -0.004)
        self.assertAlmostEqual(m.broker.cash, 1000.0 - 0.004)

    def test_restart_keeps_ratio_and_position(self):
        m = self.tracker()
        m.tick(paused=False)
        self.client.info.fills = [fill(10, "ETH", "B", 1, 100, 0)]
        m.tick(paused=False)
        self.client.book.equity = 5_000_000.0  # leader deposits: must not change our ratio
        m2 = self.tracker()
        m2.tick(paused=False)
        self.assertAlmostEqual(m2.ratio, 0.001)
        self.assertAlmostEqual(m2.broker.positions["ETH"].size, 0.001)
        self.assertEqual(m2.fills_mirrored, 1)


class TestConfig(unittest.TestCase):
    def test_copy_mode_typo_raises(self):
        with self.assertRaises(ValueError):
            make_settings(COPY_MODE="mirorr")

    def test_mirror_refuses_live(self):
        from src.bot import FuturesBot

        with tempfile.TemporaryDirectory() as tmp:
            s = make_settings(STRATEGY="copy", COPY_MODE="mirror", MODE="live", HL_TESTNET="true",
                              COPY_LEADER="0x" + "ab" * 20, LOGS_DIR=tmp)
            with self.assertRaisesRegex(RuntimeError, "paper ledger"):
                FuturesBot(s)


if __name__ == "__main__":
    unittest.main()
