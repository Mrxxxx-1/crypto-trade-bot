from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict

from .config import Settings
from .exchange import ExchangeAdapter, PaperBroker
from .risk import RiskManager
from .strategy import generate_signal, htf_trend, volume_ok


@dataclass
class SymbolState:
    last_signal: str = "flat"
    last_exit_side: str = ""
    last_exit_loop: int = 0


class FuturesBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.exchange = ExchangeAdapter(settings)
        self.risk = RiskManager(settings)
        self.broker = PaperBroker(settings, self.exchange)
        self.states: Dict[str, SymbolState] = {symbol: SymbolState() for symbol in settings.symbols}
        self.loop_count = 0

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

    def _process_symbol(self, symbol: str) -> str:
        ohlcv = self.exchange.fetch_ohlcv(symbol, self.settings.timeframe, self.settings.lookback_candles)
        signal, atr = generate_signal(ohlcv, self.settings)
        last_price = float(ohlcv[-1][4])
        atr_pct = (atr / last_price * 100) if last_price > 0 else 0.0

        # --- 1. Check pending limit orders ---
        if symbol in self.broker.pending_orders:
            if self.broker.is_pending_expired(symbol):
                self.broker.cancel_pending(symbol)
                print(f"[{self._utc_now()}] CANCEL limit order {symbol} (timeout)")
            else:
                pos = self.broker.check_pending_fill(symbol)
                if pos:
                    print(
                        f"[{self._utc_now()}] FILLED {symbol} {pos.side} size={pos.size:.6f} "
                        f"entry={pos.entry_price:.2f} equity={self.broker.equity:.2f}"
                    )
                else:
                    return f"{symbol} pending_fill limit={self.broker.pending_orders[symbol].limit_price:.2f}"

        # --- 2. Exit checks for active positions ---
        closed_by_risk = self.broker.maybe_force_exit_by_risk(symbol)
        if closed_by_risk is not None:
            self.risk.on_trade_close(closed_by_risk.pnl, self.broker.equity)
            if closed_by_risk.pnl < 0:
                state = self.states[symbol]
                state.last_exit_side = closed_by_risk.side
                state.last_exit_loop = self.loop_count
            print(f"[{self._utc_now()}] EXIT {symbol} pnl={closed_by_risk.pnl:.2f} equity={self.broker.equity:.2f}")
            self.broker.log_event(
                "risk_exit",
                {"symbol": symbol, "pnl": closed_by_risk.pnl, "equity": self.broker.equity},
            )

        if symbol in self.broker.positions:
            pos = self.broker.positions[symbol]
            return (
                f"{symbol} in_position side={pos.side} size={pos.size:.6f} "
                f"last={last_price:.2f} stop={pos.stop_price:.2f} tp={pos.take_profit_price:.2f}"
            )

        # --- 3. Check for new entry ---
        if signal == "flat" or atr <= 0:
            return f"{symbol} no_entry signal={signal} atr_pct={atr_pct:.3f} last={last_price:.2f}"

        if self.settings.volume_min_mult > 0:
            if not volume_ok(ohlcv, self.settings.atr_period, self.settings.volume_min_mult):
                return f"{symbol} no_entry low_volume signal={signal} last={last_price:.2f}"

        if self.settings.htf_timeframe:
            htf_ohlcv = self.exchange.fetch_ohlcv(
                symbol, self.settings.htf_timeframe, self.settings.lookback_candles,
            )
            htf = htf_trend(htf_ohlcv, self.settings)
            if htf != signal:
                return f"{symbol} no_entry htf_disagree signal={signal} htf={htf} last={last_price:.2f}"

        if not self.risk.can_trade(self.broker.equity):
            print(f"[{self._utc_now()}] HALT risk guard active, no new trades")
            return f"{symbol} blocked_by_risk signal={signal} atr_pct={atr_pct:.3f}"

        state = self.states[symbol]
        cd = self.settings.cooldown_candles
        if cd > 0 and state.last_exit_side:
            same_dir = (
                (signal == "long" and state.last_exit_side == "buy")
                or (signal == "short" and state.last_exit_side == "sell")
            )
            if same_dir and (self.loop_count - state.last_exit_loop) < cd:
                return f"{symbol} cooldown signal={signal} atr_pct={atr_pct:.3f} last={last_price:.2f}"

        stop_price, take_price = self._stops(signal, last_price, atr)
        size = self.risk.calc_position_size(self.broker.equity, last_price, stop_price)
        if size <= 0:
            return f"{symbol} no_entry size=0 signal={signal} atr_pct={atr_pct:.3f}"
        side = self._entry_side(signal)

        order = self.broker.place_limit_entry(
            symbol=symbol,
            side=side,
            size=size,
            limit_price=last_price,
            stop_price=stop_price,
            take_profit_price=take_price,
        )
        if order:
            print(
                f"[{self._utc_now()}] LIMIT {symbol} {side} size={size:.6f} price={last_price:.2f} "
                f"stop={stop_price:.2f} tp={take_price:.2f}"
            )
            return (
                f"{symbol} limit_placed side={side} size={size:.6f} price={last_price:.2f} "
                f"stop={stop_price:.2f} tp={take_price:.2f}"
            )
        return f"{symbol} no_entry signal={signal} atr_pct={atr_pct:.3f} last={last_price:.2f}"

    def run_forever(self) -> None:
        print(f"[{self._utc_now()}] Start bot mode={self.settings.mode} symbols={self.settings.symbols}")
        while True:
            try:
                self.loop_count += 1
                statuses = []
                for symbol in self.settings.symbols:
                    statuses.append(self._process_symbol(symbol))
                if self.loop_count % self.settings.heartbeat_interval == 0:
                    print(
                        f"[{self._utc_now()}] HEARTBEAT loop={self.loop_count} equity={self.broker.equity:.2f} "
                        f"open_positions={len(self.broker.positions)}"
                    )
                    for status in statuses:
                        print(f"  - {status}")
                    self.broker.log_event(
                        "heartbeat",
                        {
                            "loop": self.loop_count,
                            "equity": self.broker.equity,
                            "open_positions": len(self.broker.positions),
                            "statuses": statuses,
                        },
                    )
                time.sleep(self.settings.poll_seconds)
            except KeyboardInterrupt:
                print("Stopped by user.")
                break
            except Exception as exc:  # noqa: BLE001
                print(f"[{self._utc_now()}] ERROR {exc}")
                time.sleep(max(3, self.settings.poll_seconds // 2))
