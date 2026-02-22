from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict

from .config import Settings
from .exchange import ExchangeAdapter, PaperBroker
from .risk import RiskManager
from .strategy import generate_signal


@dataclass
class SymbolState:
    last_signal: str = "flat"


class FuturesBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.exchange = ExchangeAdapter(settings)
        self.risk = RiskManager(settings)
        self.broker = PaperBroker(settings, self.exchange)
        self.states: Dict[str, SymbolState] = {symbol: SymbolState() for symbol in settings.symbols}

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _entry_side(self, signal: str) -> str:
        return "buy" if signal == "long" else "sell"

    def _stops(self, signal: str, price: float, atr: float) -> tuple[float, float]:
        stop_distance = atr * self.settings.stop_atr_multiplier
        if signal == "long":
            stop = price - stop_distance
            take = price + (stop_distance * self.settings.take_profit_r)
        else:
            stop = price + stop_distance
            take = price - (stop_distance * self.settings.take_profit_r)
        return stop, take

    def _process_symbol(self, symbol: str) -> None:
        ohlcv = self.exchange.fetch_ohlcv(symbol, self.settings.timeframe, self.settings.lookback_candles)
        signal, atr = generate_signal(ohlcv, self.settings)
        last_price = float(ohlcv[-1][4])

        # Exit checks first for active positions.
        closed_by_risk = self.broker.maybe_force_exit_by_risk(symbol)
        if closed_by_risk is not None:
            self.risk.on_trade_close(closed_by_risk.pnl, self.broker.equity)
            print(f"[{self._utc_now()}] EXIT {symbol} pnl={closed_by_risk.pnl:.2f} equity={self.broker.equity:.2f}")

        if symbol in self.broker.positions:
            return
        if signal == "flat" or atr <= 0:
            return
        if not self.risk.can_trade(self.broker.equity):
            print(f"[{self._utc_now()}] HALT risk guard active, no new trades")
            return

        stop_price, take_price = self._stops(signal, last_price, atr)
        size = self.risk.calc_position_size(self.broker.equity, last_price, stop_price)
        if size <= 0:
            return
        side = self._entry_side(signal)
        pos = self.broker.open_position(
            symbol=symbol,
            side=side,
            size=size,
            stop_price=stop_price,
            take_profit_price=take_price,
        )
        if pos:
            print(
                f"[{self._utc_now()}] OPEN {symbol} {side} size={size:.6f} entry={pos.entry_price:.2f} "
                f"stop={stop_price:.2f} tp={take_price:.2f} equity={self.broker.equity:.2f}"
            )

    def run_forever(self) -> None:
        print(f"[{self._utc_now()}] Start bot mode={self.settings.mode} symbols={self.settings.symbols}")
        while True:
            try:
                for symbol in self.settings.symbols:
                    self._process_symbol(symbol)
                time.sleep(self.settings.poll_seconds)
            except KeyboardInterrupt:
                print("Stopped by user.")
                break
            except Exception as exc:  # noqa: BLE001
                print(f"[{self._utc_now()}] ERROR {exc}")
                time.sleep(max(3, self.settings.poll_seconds // 2))
