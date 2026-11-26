"""Tests for the pure metrics in ``src.leader_scan`` (no network).

Round-trip reconstruction is the part most likely to be quietly wrong: a missed
flip or a mid-window open would mis-state win rate and hold time for every
candidate, so those cases are pinned here.
"""

from __future__ import annotations

import math
import unittest

from src.leader_scan import (
    R_FEW_TRADES,
    R_MAKER,
    R_NEGATIVE,
    R_SHORT_HOLD,
    R_EMPTY,
    R_TOO_ACTIVE,
    Candidate,
    FillStats,
    PortfolioStats,
    Result,
    fill_stats,
    parse_leaderboard,
    portfolio_stats,
    prefilter,
    reject_reason,
    round_trips,
    score,
)

H = 3_600_000


def fill(t_h, side, sz, start, px=100.0, pnl=0.0, fee=0.0, coin="BTC", crossed=True, tid=None):
    return {
        "coin": coin,
        "time": int(t_h * H),
        "side": side,
        "sz": str(sz),
        "px": str(px),
        "startPosition": str(start),
        "closedPnl": str(pnl),
        "fee": str(fee),
        "crossed": crossed,
        "tid": tid if tid is not None else int(t_h * 1000),
    }


class TestRoundTrips(unittest.TestCase):
    def test_open_add_close(self):
        fills = [
            fill(0, "B", 1, 0, fee=0.1),
            fill(1, "B", 1, 1, fee=0.1),
            fill(5, "A", 2, 2, px=110, pnl=20, fee=0.2),
        ]
        done, still_open = round_trips(fills)
        self.assertEqual(still_open, [])
        self.assertEqual(len(done), 1)
        rt = done[0]
        self.assertEqual(rt.side, "long")
        self.assertAlmostEqual(rt.hold_hours, 5)
        self.assertAlmostEqual(rt.net(), 20 - 0.4)
        self.assertAlmostEqual(rt.notional, 100 + 100 + 220)
        self.assertEqual(rt.max_size, 2)

    def test_flip_splits_the_fill(self):
        # long 1 -> sell 3 -> short 2 -> buy 2 -> flat
        fills = [
            fill(0, "B", 1, 0),
            fill(2, "A", 3, 1, pnl=5, fee=0.3),
            fill(6, "B", 2, -2, pnl=-1),
        ]
        done, still_open = round_trips(fills)
        self.assertEqual(still_open, [])
        self.assertEqual([rt.side for rt in done], ["long", "short"])
        long_rt, short_rt = done
        self.assertAlmostEqual(long_rt.closed_pnl, 5)  # the flip's closedPnl belongs to the close
        self.assertAlmostEqual(long_rt.fees, 0.1)  # 1/3 of the flip fill's fee
        self.assertAlmostEqual(short_rt.fees, 0.2)
        self.assertAlmostEqual(short_rt.hold_hours, 4)
        self.assertEqual(short_rt.max_size, 2)

    def test_mid_position_start_has_no_hold_time(self):
        done, _ = round_trips([fill(3, "A", 1, 1, pnl=2)])
        self.assertEqual(len(done), 1)
        self.assertFalse(done[0].open_observed)
        self.assertIsNone(done[0].hold_hours)

    def test_unclosed_position_is_open(self):
        done, still_open = round_trips([fill(0, "A", 1, 0)])
        self.assertEqual(done, [])
        self.assertEqual(still_open[0].side, "short")

    def test_spot_fills_ignored_and_coins_independent(self):
        fills = [
            fill(0, "B", 1, 0, coin="@107"),
            fill(0, "B", 1, 0, coin="ETH"),
            fill(1, "B", 1, 0, coin="BTC"),
            fill(2, "A", 1, 1, coin="ETH"),
            fill(3, "A", 1, 1, coin="BTC"),
        ]
        done, still_open = round_trips(fills)
        self.assertEqual(sorted(rt.coin for rt in done), ["BTC", "ETH"])
        self.assertEqual(still_open, [])

    def test_float_dust_counts_as_flat(self):
        done, still_open = round_trips([fill(0, "B", 0.3, 0), fill(1, "A", 0.1, 0.3), fill(2, "A", 0.2, 0.2)])
        self.assertEqual(len(done), 1)
        self.assertEqual(still_open, [])


class TestFillStats(unittest.TestCase):
    def test_copy_cost_turns_a_scalper_negative(self):
        # 10 round trips earning 1 bp each on $100 notional per side; 5 bps cost kills it.
        fills = []
        for i in range(10):
            fills.append(fill(i, "B", 1, 0))
            fills.append(fill(i + 0.5, "A", 1, 1, px=100.01, pnl=0.01))
        st = fill_stats(fills, window_days=1, copy_cost_bps=5)
        self.assertEqual(st.round_trips, 10)
        self.assertGreater(st.realized_pnl, 0)
        self.assertLess(st.pnl_after_cost, 0)
        self.assertEqual(st.win_rate, 0.0)
        self.assertEqual(st.profit_factor, 0.0)
        self.assertAlmostEqual(st.median_hold_hours, 0.5)
        self.assertAlmostEqual(st.trades_per_day, 10)

    def test_maker_ratio_and_concentration(self):
        fills = [
            fill(0, "B", 1, 0, crossed=False),
            fill(1, "A", 1, 1, pnl=100),
            fill(2, "B", 1, 0, crossed=False),
            fill(3, "A", 1, 1, pnl=-10),
        ]
        st = fill_stats(fills, window_days=10, copy_cost_bps=0)
        self.assertAlmostEqual(st.maker_ratio, 0.5)
        self.assertEqual(st.hip3_share, 0.0)
        self.assertAlmostEqual(st.profit_factor, 10)
        self.assertAlmostEqual(st.win_rate, 0.5)
        self.assertAlmostEqual(st.pnl_without_top3, 0)  # only 2 trips: all in the top 3
        self.assertEqual(st.long_share, 1.0)

    def test_hip3_share(self):
        fills = [fill(0, "B", 1, 0, coin="xyz:NVDA"), fill(1, "B", 3, 0, coin="BTC")]
        self.assertAlmostEqual(fill_stats(fills, window_days=1, copy_cost_bps=0).hip3_share, 0.25)

    def test_no_losers_is_infinite_profit_factor(self):
        st = fill_stats([fill(0, "B", 1, 0), fill(1, "A", 1, 1, pnl=5)], window_days=1, copy_cost_bps=0)
        self.assertTrue(math.isinf(st.profit_factor))


class TestPortfolio(unittest.TestCase):
    def _pf(self, points):
        # points: (day, account_value, cumulative_pnl)
        ms = lambda d: 1_767_225_600_000 + d * 86_400_000  # 2026-01-01  # noqa: E731
        return [
            [
                "allTime",
                {
                    "accountValueHistory": [[ms(d), str(av)] for d, av, _ in points],
                    "pnlHistory": [[ms(d), str(p)] for d, _, p in points],
                },
            ]
        ]

    def test_withdrawal_is_not_a_drawdown(self):
        # A withdrawal halves account value with no P&L change.
        st = portfolio_stats(self._pf([(0, 1000, 0), (10, 500, 0), (20, 600, 100)]))
        self.assertEqual(st.max_drawdown_pct, 0.0)
        self.assertAlmostEqual(st.twr, 0.2)  # +100 on 500 of capital

    def test_deposit_is_not_a_gain(self):
        st = portfolio_stats(self._pf([(0, 1000, 0), (10, 5000, 0), (20, 4500, -500)]))
        self.assertAlmostEqual(st.max_drawdown_pct, 0.10)

    def test_drawdown_is_time_weighted(self):
        # +20%, then -25%: index 1.2 -> 0.9, a 25% drawdown, still 25% below peak.
        st = portfolio_stats(self._pf([(0, 1000, 0), (10, 1200, 200), (20, 900, -100)]))
        self.assertAlmostEqual(st.max_drawdown_pct, 0.25)
        self.assertAlmostEqual(st.current_drawdown_pct, 0.25)

    def test_losses_after_withdrawing_profits_stay_below_100pct(self):
        # The old peak-P&L / equity-at-peak formula reported >100% here.
        st = portfolio_stats(self._pf([(0, 1000, 0), (10, 3000, 2000), (20, 500, 2000), (30, 100, 1600)]))
        self.assertAlmostEqual(st.max_drawdown_pct, 0.8)

    def test_empty_account_interval_is_skipped(self):
        st = portfolio_stats(self._pf([(0, 0, 0), (10, 1000, 0), (20, 1100, 100)]))
        self.assertAlmostEqual(st.twr, 0.1)

    def test_monthly_consistency(self):
        # Jan +100, Feb -50, Mar +80
        st = portfolio_stats(self._pf([(0, 1000, 0), (30, 1100, 100), (58, 1050, 50), (88, 1130, 130)]))
        self.assertEqual((st.profitable_months, st.months), (2, 3))
        self.assertAlmostEqual(st.track_days, 88)


class TestScreening(unittest.TestCase):
    def _cand(self, **kw):
        base = dict(
            address="0xa", lb_equity=100_000, month_pnl=1, month_roi=0.01,
            month_volume=1_000_000, all_time_pnl=10, all_time_roi=0.1,
        )
        base.update(kw)
        return Candidate(**base)

    def test_parse_leaderboard(self):
        rows = [
            {
                "ethAddress": "0xABC",
                "accountValue": "123.5",
                "windowPerformances": [
                    ["month", {"pnl": "1", "roi": "0.1", "vlm": "50"}],
                    ["allTime", {"pnl": "9", "roi": "0.5", "vlm": "500"}],
                ],
            },
            {"ethAddress": "0xbad"},  # malformed: skipped, not a crash
        ]
        c = parse_leaderboard(rows)
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0].address, "0xabc")
        self.assertAlmostEqual(c[0].turnover, 50 / 123.5)

    def test_prefilter(self):
        cands = [
            self._cand(address="0xgood", all_time_pnl=50),
            self._cand(address="0xbetter", all_time_pnl=90),
            self._cand(address="0xvault"),
            self._cand(address="0xsmall", lb_equity=1000),
            self._cand(address="0xloser", month_pnl=-1),
            self._cand(address="0xmm", month_volume=1e9),  # turnover 10,000x
        ]
        kept = prefilter(
            cands, vault_addresses={"0xvault"}, min_equity=25_000, max_equity=1e9, max_turnover=300
        )
        self.assertEqual([c.address for c in kept], ["0xbetter", "0xgood"])

    def _result(self, **fs):
        f = FillStats(round_trips=20, median_hold_hours=10, maker_ratio=0.2, realized_pnl=1000)
        for k, v in fs.items():
            setattr(f, k, v)
        return Result(candidate=self._cand(), own_equity=100_000, fills=f, portfolio=PortfolioStats())

    def test_reject_reasons(self):
        kw = dict(min_trades=10, min_hold_hours=1, max_maker_ratio=0.8, min_equity=25_000)
        self.assertIsNone(reject_reason(self._result(), **kw))
        emptied = self._result()
        emptied.own_equity = 0.0
        self.assertEqual(reject_reason(emptied, **kw), R_EMPTY)
        self.assertEqual(reject_reason(self._result(truncated=True), **kw), R_TOO_ACTIVE)
        self.assertEqual(reject_reason(self._result(round_trips=3), **kw), R_FEW_TRADES)
        self.assertEqual(reject_reason(self._result(median_hold_hours=0.2), **kw), R_SHORT_HOLD)
        self.assertEqual(reject_reason(self._result(median_hold_hours=None), **kw), R_SHORT_HOLD)
        self.assertEqual(reject_reason(self._result(maker_ratio=0.95), **kw), R_MAKER)
        self.assertEqual(reject_reason(self._result(copy_cost=2000), **kw), R_NEGATIVE)

    def test_score_bounds(self):
        best = self._result(profit_factor=math.inf, top3_share=0.0, realized_pnl=1e6)
        best.portfolio = PortfolioStats(max_drawdown_pct=0, months=6, profitable_months=6)
        s, parts = score(best)
        self.assertEqual(s, 100.0)
        worst = self._result(profit_factor=0.5, top3_share=math.inf, realized_pnl=0)
        worst.portfolio = PortfolioStats(max_drawdown_pct=0.9, months=6, profitable_months=0)
        s, _ = score(worst)
        self.assertEqual(s, 0.0)


if __name__ == "__main__":
    unittest.main()
