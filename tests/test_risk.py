"""Tests for the drawdown / loss-streak halts in ``src.risk``.

The regression these exist to pin down: the drawdown baseline used to be seeded
from ``INITIAL_EQUITY`` unconditionally, so a live account whose balance had
drifted below that number was halted before it could place a single trade.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from src.risk import RiskManager
from tests.helpers import make_settings


class TestDrawdownBaseline(unittest.TestCase):
    def test_defaults_to_initial_equity(self):
        """Omitting starting_equity keeps the old paper-mode behaviour."""
        rm = RiskManager(make_settings(INITIAL_EQUITY=1000))
        self.assertEqual(rm.state.window_start_equity, 1000)

    def test_explicit_starting_equity_wins(self):
        rm = RiskManager(make_settings(INITIAL_EQUITY=1000), starting_equity=942.62)
        self.assertEqual(rm.state.window_start_equity, 942.62)

    def test_zero_starting_equity_is_honoured(self):
        """0.0 must not be mistaken for 'unset' by a falsy check."""
        rm = RiskManager(make_settings(INITIAL_EQUITY=1000), starting_equity=0.0)
        self.assertEqual(rm.state.window_start_equity, 0.0)

    def test_stale_initial_equity_halts_before_first_trade(self):
        """The live bug: account below a stale INITIAL_EQUITY halts immediately."""
        settings = make_settings(INITIAL_EQUITY=1000, MAX_DAILY_LOSS_PCT=2.0)
        rm = RiskManager(settings)
        self.assertFalse(rm.can_trade(942.62))

    def test_seeding_from_account_allows_trading(self):
        """Same account, baseline taken from the broker: free to trade."""
        settings = make_settings(INITIAL_EQUITY=1000, MAX_DAILY_LOSS_PCT=2.0)
        rm = RiskManager(settings, starting_equity=942.62)
        self.assertTrue(rm.can_trade(942.62))


class TestHaltsStillFire(unittest.TestCase):
    """Seeding from the account must not defang the guards."""

    def test_real_drawdown_from_seeded_baseline_halts(self):
        settings = make_settings(INITIAL_EQUITY=1000, MAX_DAILY_LOSS_PCT=10.0)
        rm = RiskManager(settings, starting_equity=942.62)
        self.assertTrue(rm.can_trade(900.00))       # -4.5%, inside the guard
        self.assertFalse(rm.can_trade(848.00))      # -10.0%, breached
        self.assertIsNotNone(rm.state.halted_until)

    def test_consecutive_losses_halt(self):
        settings = make_settings(
            INITIAL_EQUITY=1000, MAX_DAILY_LOSS_PCT=100, MAX_CONSECUTIVE_LOSSES=3
        )
        rm = RiskManager(settings, starting_equity=1000)
        for _ in range(3):
            rm.on_trade_close(pnl=-5.0, equity=1000)
        self.assertFalse(rm.can_trade(1000))

    def test_a_win_resets_the_streak(self):
        settings = make_settings(
            INITIAL_EQUITY=1000, MAX_DAILY_LOSS_PCT=100, MAX_CONSECUTIVE_LOSSES=3
        )
        rm = RiskManager(settings, starting_equity=1000)
        rm.on_trade_close(pnl=-5.0, equity=1000)
        rm.on_trade_close(pnl=-5.0, equity=1000)
        rm.on_trade_close(pnl=+9.0, equity=1000)
        self.assertEqual(rm.state.consecutive_losses, 0)
        self.assertTrue(rm.can_trade(1000))

    def test_expired_halt_rebaselines_to_current_equity(self):
        """After a cooldown the window restarts from wherever equity now sits."""
        settings = make_settings(INITIAL_EQUITY=1000, MAX_DAILY_LOSS_PCT=10.0)
        rm = RiskManager(settings, starting_equity=1000)
        self.assertFalse(rm.can_trade(880.0))
        rm.state.halted_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertTrue(rm.can_trade(880.0))
        self.assertEqual(rm.state.window_start_equity, 880.0)


if __name__ == "__main__":
    unittest.main()
