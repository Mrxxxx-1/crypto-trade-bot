from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import Settings


@dataclass
class RiskState:
    day: date
    day_start_equity: float
    consecutive_losses: int = 0
    halted: bool = False


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        today = date.today()
        self.state = RiskState(day=today, day_start_equity=settings.initial_equity)

    def _roll_day_if_needed(self, equity: float) -> None:
        today = date.today()
        if today != self.state.day:
            carry = self.state.consecutive_losses
            self.state = RiskState(
                day=today,
                day_start_equity=equity,
                consecutive_losses=carry,
            )

    def can_trade(self, equity: float) -> bool:
        self._roll_day_if_needed(equity)
        if self.state.halted:
            return False

        day_drawdown_pct = ((self.state.day_start_equity - equity) / max(self.state.day_start_equity, 1e-9)) * 100
        if day_drawdown_pct >= self.settings.max_daily_loss_pct:
            self.state.halted = True
            return False

        if self.state.consecutive_losses >= self.settings.max_consecutive_losses:
            self.state.halted = True
            return False
        return True

    def on_trade_close(self, pnl: float, equity: float) -> None:
        self._roll_day_if_needed(equity)
        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

    def calc_position_size(self, equity: float, entry_price: float, stop_price: float) -> float:
        risk_amount = equity * (self.settings.risk_per_trade_pct / 100)
        per_unit_risk = abs(entry_price - stop_price)
        if per_unit_risk <= 0:
            return 0.0
        raw_size = risk_amount / per_unit_risk

        max_notional = equity * self.settings.max_leverage
        max_size_by_leverage = max_notional / max(entry_price, 1e-9)

        return max(0.0, min(raw_size, max_size_by_leverage))
