"""Risk controls: position sizing and timer-based trading halts.

Two halt triggers:
- **Consecutive losses** >= ``MAX_CONSECUTIVE_LOSSES`` -> halt for
  ``CONSEC_HALT_HOURS``.
- **Rolling drawdown** >= ``MAX_DAILY_LOSS_PCT`` from the window start
  equity -> halt for ``DAILY_LOSS_HALT_HOURS``.

"Daily" is a rolling window that resets each time a halt expires -- not a
calendar-day boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import Settings


@dataclass
class RiskState:
    """Mutable state tracked by ``RiskManager``.

    ``window_start_equity`` resets whenever a halt timer expires, giving
    the strategy a clean slate after each cooldown period.
    """
    window_start_equity: float
    consecutive_losses: int = 0
    halted_until: Optional[datetime] = field(default=None)


class RiskManager:
    """Enforce per-trade sizing, drawdown limits, and consecutive-loss halts."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = RiskState(
            window_start_equity=settings.initial_equity,
        )

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _check_halt_expired(self, equity: float) -> None:
        if self.state.halted_until is None:
            return
        if self._now() >= self.state.halted_until:
            self.state = RiskState(
                window_start_equity=equity,
            )

    def can_trade(self, equity: float) -> bool:
        """Return False if halted, drawdown breached, or loss streak exceeded."""
        self._check_halt_expired(equity)

        if self.state.halted_until is not None:
            return False

        dd_pct = ((self.state.window_start_equity - equity) / max(self.state.window_start_equity, 1e-9)) * 100
        if dd_pct >= self.settings.max_daily_loss_pct:
            self._halt(self.settings.daily_loss_halt_hours)
            return False

        if self.state.consecutive_losses >= self.settings.max_consecutive_losses:
            self._halt(self.settings.consec_halt_hours)
            return False

        return True

    def _halt(self, hours: float) -> None:
        self.state.halted_until = self._now() + timedelta(hours=hours)

    def on_trade_close(self, pnl: float, equity: float) -> None:
        """Update streak: increment on loss, reset to 0 on win."""
        self._check_halt_expired(equity)
        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

    def calc_position_size(self, equity: float, entry_price: float, stop_price: float) -> float:
        """Size = risk_amount / per_unit_risk, capped by max_leverage * equity.

        Deprecated under the DCA strategy (no hard SL).  Retained so external
        callers / older code paths keep working.  Use ``calc_leg_size`` instead.
        """
        risk_amount = equity * (self.settings.risk_per_trade_pct / 100)
        per_unit_risk = abs(entry_price - stop_price)
        if per_unit_risk <= 0:
            return 0.0
        raw_size = risk_amount / per_unit_risk

        max_notional = equity * self.settings.max_leverage
        max_size_by_leverage = max_notional / max(entry_price, 1e-9)

        return max(0.0, min(raw_size, max_size_by_leverage))

    def calc_leg_size(self, starting_equity: float, current_price: float) -> float:
        """DCA leg sizing: each leg = ``leg_notional_pct`` of *starting* equity.

        Using starting equity (not current equity) keeps every leg the same notional
        regardless of unrealized P/L, which is what "DCA at -X% from last fill"
        intuitively means.  Still capped at ``max_leverage * starting_equity``
        worth of size per leg as a sanity guard.
        """
        if current_price <= 0 or starting_equity <= 0:
            return 0.0
        notional = starting_equity * (self.settings.leg_notional_pct / 100.0)
        size = notional / current_price
        max_size = (starting_equity * self.settings.max_leverage) / current_price
        return max(0.0, min(size, max_size))
