"""Main trading loop: poll OHLCV, generate signals from closed candles,
check exits via current candle high/low, manage limit entries, cooldown,
and risk halts.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

from .config import Settings
from .exchange import ExchangeAdapter, PaperBroker
from .models import Side
from .risk import RiskManager
from .strategy import generate_signal, htf_trend, volume_ok


@dataclass
class SymbolState:
    """Per-symbol state for cooldown tracking.

    After a stop exit, ``last_exit_ts`` and ``last_exit_side`` enforce a
    ``COOLDOWN_CANDLES * timeframe_minutes`` wait before re-entering the
    same direction.
    """
    last_signal: str = "flat"
    last_exit_side: str = ""
    last_exit_ts: Optional[datetime] = None


class FuturesBot:
    """Paper futures bot: polls OKX, runs strategy, and manages risk per tick."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.exchange = ExchangeAdapter(settings)
        self.risk = RiskManager(settings)
        self.broker = PaperBroker(settings, self.exchange)
        self.states: Dict[str, SymbolState] = {symbol: SymbolState() for symbol in settings.symbols}
        self.loop_count = 0

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _entry_side(self, signal: str) -> Side:
        return "buy" if signal == "long" else "sell"

    def _stops(self, signal: str, price: float, atr: float) -> tuple[float, float]:
        """Return ``(stop_price, take_profit_price)`` based on ATR and ``TAKE_PROFIT_R``."""
        stop_distance = atr * self.settings.stop_atr_multiplier
        if signal == "long":
            stop = price - stop_distance
            take = price + (stop_distance * self.settings.take_profit_r)
        else:
            stop = price + stop_distance
            take = price - (stop_distance * self.settings.take_profit_r)
        return stop, take

    def _timeframe_minutes(self) -> int:
        """Parse timeframe string (e.g. ``5m``, ``1h``) into total minutes."""
        tf = self.settings.timeframe
        if tf.endswith("m"):
            return int(tf[:-1])
        if tf.endswith("h"):
            return int(tf[:-1]) * 60
        if tf.endswith("d"):
            return int(tf[:-1]) * 1440
        return 5

    def _process_symbol(self, symbol: str) -> str:
        """Process one symbol per tick.

        1. Use closed candles for signal generation, current candle for exits.
        2. Check / fill / timeout pending limit orders.
        3. Exit via candle high/low against stop/TP (skip entry candle).
        4. Enter new positions after cooldown, risk, and filter checks.
        """
        ohlcv = self.exchange.fetch_ohlcv(
            symbol, self.settings.timeframe, self.settings.lookback_candles + 1,
        )
        if len(ohlcv) < 2:
            return f"{symbol} insufficient_data"

        # Closed candles for signal; last candle (possibly forming) for exit checks
        closed_candles = ohlcv[:-1]
        current_candle = ohlcv[-1]
        current_candle_ts = datetime.fromtimestamp(
            current_candle[0] / 1000, tz=timezone.utc,
        )
        current_high = float(current_candle[2])
        current_low = float(current_candle[3])
        last_close = float(closed_candles[-1][4])

        signal, atr = generate_signal(closed_candles, self.settings)
        atr_pct = (atr / last_close * 100) if last_close > 0 else 0.0

        # --- 1. Check pending limit orders ---
        if symbol in self.broker.pending_orders:
            if self.broker.is_pending_expired(symbol):
                self.broker.cancel_pending(symbol)
                print(f"[{self._utc_now()}] CANCEL limit order {symbol} (timeout)")
            else:
                pos = self.broker.check_pending_fill(symbol)
                if pos:
                    pos.entry_candle_ts = current_candle_ts
                    print(
                        f"[{self._utc_now()}] FILLED {symbol} {pos.side} size={pos.size:.6f} "
                        f"entry={pos.entry_price:.2f} equity={self.broker.equity:.2f}"
                    )
                else:
                    return f"{symbol} pending_fill limit={self.broker.pending_orders[symbol].limit_price:.2f}"

        # --- 2. Exit checks for active positions ---
        if symbol in self.broker.positions:
            pos = self.broker.positions[symbol]

            # Skip exit on entry candle (matches backtest)
            if pos.entry_candle_ts is not None and current_candle_ts <= pos.entry_candle_ts:
                return (
                    f"{symbol} in_position side={pos.side} size={pos.size:.6f} "
                    f"last={last_close:.2f} stop={pos.stop_price:.2f} tp={pos.take_profit_price:.2f} (entry_candle)"
                )

            # Check stop/TP using candle high/low (matches backtest)
            hit_stop = hit_tp = False
            if pos.side == "buy":
                hit_stop = current_low <= pos.stop_price
                hit_tp = current_high >= pos.take_profit_price
            else:
                hit_stop = current_high >= pos.stop_price
                hit_tp = current_low <= pos.take_profit_price

            if hit_stop or hit_tp:
                if hit_stop:
                    exit_at = pos.stop_price
                    reason = "stop"
                else:
                    exit_at = pos.take_profit_price
                    reason = "tp"

                trade = self.broker.close_position_at(symbol, exit_at, reason)
                if trade:
                    self.risk.on_trade_close(trade.pnl, self.broker.equity)
                    if reason == "stop":
                        state = self.states[symbol]
                        state.last_exit_side = trade.side
                        state.last_exit_ts = datetime.now(timezone.utc)
                    print(
                        f"[{self._utc_now()}] EXIT {symbol} {reason} pnl={trade.pnl:.2f} "
                        f"equity={self.broker.equity:.2f}"
                    )

            if symbol in self.broker.positions:
                pos = self.broker.positions[symbol]
                return (
                    f"{symbol} in_position side={pos.side} size={pos.size:.6f} "
                    f"last={last_close:.2f} stop={pos.stop_price:.2f} tp={pos.take_profit_price:.2f}"
                )

        # --- 3. Check for new entry ---
        if signal == "flat" or atr <= 0:
            return f"{symbol} no_entry signal={signal} atr_pct={atr_pct:.3f} last={last_close:.2f}"

        if self.settings.volume_min_mult > 0:
            if not volume_ok(closed_candles, self.settings.atr_period, self.settings.volume_min_mult):
                return f"{symbol} no_entry low_volume signal={signal} last={last_close:.2f}"

        if self.settings.htf_timeframe:
            htf_ohlcv = self.exchange.fetch_ohlcv(
                symbol, self.settings.htf_timeframe, self.settings.lookback_candles + 1,
            )
            if len(htf_ohlcv) > 1:
                htf_closed = htf_ohlcv[:-1]
            else:
                htf_closed = htf_ohlcv
            htf = htf_trend(htf_closed, self.settings)
            if htf != signal:
                return f"{symbol} no_entry htf_disagree signal={signal} htf={htf} last={last_close:.2f}"

        if not self.risk.can_trade(self.broker.equity):
            print(f"[{self._utc_now()}] HALT risk guard active, no new trades")
            return f"{symbol} blocked_by_risk signal={signal} atr_pct={atr_pct:.3f}"

        state = self.states[symbol]
        cd_minutes = self.settings.cooldown_candles * self._timeframe_minutes()
        if cd_minutes > 0 and state.last_exit_ts is not None:
            same_dir = (
                (signal == "long" and state.last_exit_side == "buy")
                or (signal == "short" and state.last_exit_side == "sell")
            )
            elapsed_min = (datetime.now(timezone.utc) - state.last_exit_ts).total_seconds() / 60
            if same_dir and elapsed_min < cd_minutes:
                return f"{symbol} cooldown signal={signal} atr_pct={atr_pct:.3f} last={last_close:.2f}"

        stop_price, take_price = self._stops(signal, last_close, atr)
        size = self.risk.calc_position_size(self.broker.equity, last_close, stop_price)
        if size <= 0:
            return f"{symbol} no_entry size=0 signal={signal} atr_pct={atr_pct:.3f}"
        side = self._entry_side(signal)

        order = self.broker.place_limit_entry(
            symbol=symbol,
            side=side,
            size=size,
            limit_price=last_close,
            stop_price=stop_price,
            take_profit_price=take_price,
        )
        if order:
            print(
                f"[{self._utc_now()}] LIMIT {symbol} {side} size={size:.6f} price={last_close:.2f} "
                f"stop={stop_price:.2f} tp={take_price:.2f}"
            )
            return (
                f"{symbol} limit_placed side={side} size={size:.6f} price={last_close:.2f} "
                f"stop={stop_price:.2f} tp={take_price:.2f}"
            )
        return f"{symbol} no_entry signal={signal} atr_pct={atr_pct:.3f} last={last_close:.2f}"

    def run_forever(self) -> None:
        """Main loop: process all symbols, sleep, log heartbeats."""
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
