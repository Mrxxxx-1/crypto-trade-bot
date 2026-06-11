"""Main trading loop: long-only DCA-on-dips with trailing-stop exit.

Per tick, per symbol:
  1. Fetch OHLCV (closed candles for triggers, current candle high/low for trail checks).
  2. If a position is open: maybe arm or update the trailing stop; exit (all legs)
     if armed and price <= trail level; otherwise consider a DCA add if last close has
     dropped >= ``dca_trigger_pct`` below the most recent leg's fill price.
  3. If no position: enter the first leg if price is in zone, the entry trigger fires,
     and risk guards allow new trades.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict

from . import control
from .config import Settings
from .exchange import ExchangeAdapter, LiveBroker, PaperBroker
from .risk import RiskManager
from .strategy import (
    dca_trigger,
    entry_trigger,
    in_trend,
    local_extreme,
    price_in_zone,
    should_arm_trail,
    stop_loss_price,
    take_profit_price,
    trail_stop_price,
)
from .strategy_trend import (
    atr_value,
    chandelier_stop,
    initial_stop,
    position_size,
    regime_intact,
    trend_signal,
)


class FuturesBot:
    """Long-only DCA bot: polls Hyperliquid, scales into dips, exits via trailing stop."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.exchange = ExchangeAdapter(settings)
        self.risk = RiskManager(settings)
        if settings.is_live:
            self.broker: PaperBroker | LiveBroker = LiveBroker(settings, self.exchange)
        else:
            self.broker = PaperBroker(settings, self.exchange)
        self.starting_equity = settings.initial_equity
        self.loop_count = 0
        # Cross-process pause flag (toggled by the Telegram/MCP agent layer).
        # Re-read once per loop in run_forever; only blocks NEW entries/adds.
        self.paused = control.is_paused(settings.logs_dir)

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _positions_snapshot(self) -> list[dict]:
        """Structured view of open positions for heartbeat logging.

        Consumed by the dashboard / MCP / Telegram read layer so they always
        report the bot's actual current positions (not parsed from text).
        """
        snap = []
        for symbol, pos in self.broker.positions.items():
            snap.append(
                {
                    "symbol": symbol,
                    "side": pos.side,
                    "legs": len(pos.legs),
                    "max_legs": self.settings.max_dca_legs,
                    "size": round(pos.size, 8),
                    "avg_entry": round(pos.entry_price, 4),
                    "last_fill": round(pos.last_fill_price, 4),
                    "trail_armed": pos.trail_armed,
                    "peak_price": round(pos.peak_price, 4),
                    "stop_price": round(pos.stop_price, 4),
                    "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
                }
            )
        return snap

    def _unrealized_pct(self, avg_entry: float, last_close: float) -> float:
        if avg_entry <= 0:
            return 0.0
        return (last_close / avg_entry - 1.0) * 100.0

    def _process_symbol(self, symbol: str) -> str:
        """Process one symbol per tick.

        Returns a short status string for heartbeat logging.
        """
        if self.settings.strategy == "trend":
            return self._process_symbol_trend(symbol)

        ohlcv = self.exchange.fetch_ohlcv(
            symbol,
            self.settings.timeframe,
            self.settings.lookback_candles + 1,
        )
        if len(ohlcv) < 2:
            return f"{symbol} insufficient_data"

        closed_candles = ohlcv[:-1]
        current_candle = ohlcv[-1]
        current_candle_ts = datetime.fromtimestamp(
            current_candle[0] / 1000,
            tz=timezone.utc,
        )
        current_high = float(current_candle[2])
        current_low = float(current_candle[3])
        last_close = float(closed_candles[-1][4])

        direction = self.settings.direction_for(symbol)
        is_long = direction != "short"
        side = "buy" if is_long else "sell"

        # --- 1. Manage open position (TP / trail / stop / DCA) ---
        if symbol in self.broker.positions:
            pos = self.broker.positions[symbol]

            # Fixed take-profit: exit all legs once the bar reaches the target.
            tp_price = take_profit_price(pos.entry_price, self.settings, direction)
            tp_hit = tp_price > 0 and (current_high >= tp_price if is_long else current_low <= tp_price)
            if tp_hit:
                trade = self.broker.close_all_legs(symbol, tp_price, "tp")
                if trade:
                    self.risk.on_trade_close(trade.pnl, self.broker.equity)
                    print(
                        f"[{self._utc_now()}] EXIT {symbol} tp pnl={trade.pnl:+.2f} "
                        f"legs={trade.legs} avg={trade.entry_price:.2f} exit={trade.exit_price:.2f} "
                        f"equity={self.broker.equity:.2f}"
                    )
                    return f"{symbol} exited tp pnl={trade.pnl:+.2f} legs={trade.legs}"

            # Arm trailing once price has moved favorably by trail_activate_pct.
            if not pos.trail_armed and should_arm_trail(pos.entry_price, last_close, self.settings, direction):
                pos.trail_armed = True
                pos.peak_price = (
                    max(pos.peak_price, current_high, last_close)
                    if is_long
                    else min(pos.peak_price, current_low, last_close)
                )
                pos.stop_price = trail_stop_price(pos.peak_price, self.settings, direction)
                self.broker.log_event(
                    "trail_armed",
                    {
                        "symbol": symbol,
                        "direction": direction,
                        "avg_entry_price": pos.entry_price,
                        "last_close": last_close,
                        "peak_price": pos.peak_price,
                        "stop_price": pos.stop_price,
                    },
                )
                print(
                    f"[{self._utc_now()}] TRAIL ARMED {symbol} ({direction}) avg={pos.entry_price:.4f} "
                    f"ext={pos.peak_price:.4f} stop={pos.stop_price:.4f}"
                )

            if pos.trail_armed:
                # Ratchet the favorable extreme, then exit if price reverses to the stop.
                if is_long and current_high > pos.peak_price:
                    pos.peak_price = current_high
                    pos.stop_price = trail_stop_price(pos.peak_price, self.settings, direction)
                elif not is_long and current_low < pos.peak_price:
                    pos.peak_price = current_low
                    pos.stop_price = trail_stop_price(pos.peak_price, self.settings, direction)
                trail_hit = current_low <= pos.stop_price if is_long else current_high >= pos.stop_price
                if trail_hit:
                    trade = self.broker.close_all_legs(symbol, pos.stop_price, "trail")
                    if trade:
                        self.risk.on_trade_close(trade.pnl, self.broker.equity)
                        print(
                            f"[{self._utc_now()}] EXIT {symbol} trail pnl={trade.pnl:+.2f} "
                            f"legs={trade.legs} avg={trade.entry_price:.2f} exit={trade.exit_price:.2f} "
                            f"equity={self.broker.equity:.2f}"
                        )
                        return (
                            f"{symbol} exited trail pnl={trade.pnl:+.2f} legs={trade.legs}"
                        )

            # Hard stop-loss: cap catastrophic adverse moves (independent of trail).
            if symbol in self.broker.positions:
                pos = self.broker.positions[symbol]
                sl_price = stop_loss_price(pos.entry_price, self.settings, direction)
                sl_hit = sl_price > 0 and (current_low <= sl_price if is_long else current_high >= sl_price)
                if sl_hit:
                    trade = self.broker.close_all_legs(symbol, sl_price, "stop")
                    if trade:
                        self.risk.on_trade_close(trade.pnl, self.broker.equity)
                        print(
                            f"[{self._utc_now()}] EXIT {symbol} stop pnl={trade.pnl:+.2f} "
                            f"legs={trade.legs} avg={trade.entry_price:.2f} exit={trade.exit_price:.2f} "
                            f"equity={self.broker.equity:.2f}"
                        )
                        return f"{symbol} exited stop pnl={trade.pnl:+.2f} legs={trade.legs}"

            # if still open, consider a DCA add (further in the adverse direction)
            if symbol in self.broker.positions:
                pos = self.broker.positions[symbol]
                if not self.paused and len(pos.legs) < self.settings.max_dca_legs:
                    if dca_trigger(last_close, pos.last_fill_price, self.settings, direction) and in_trend(closed_candles, self.settings, direction):
                        if price_in_zone(symbol, last_close, self.settings, direction):
                            if self.risk.can_trade(self.broker.equity):
                                add_size = self.risk.calc_leg_size(
                                    self.starting_equity, last_close
                                )
                                if add_size > 0:
                                    new_pos = self.broker.open_leg(
                                        symbol, side, add_size, last_close
                                    )
                                    if new_pos:
                                        print(
                                            f"[{self._utc_now()}] DCA ADD {symbol} ({direction}) leg={len(new_pos.legs)}/{self.settings.max_dca_legs} "
                                            f"size={add_size:.6f} px={last_close:.4f} "
                                            f"avg={new_pos.entry_price:.4f} equity={self.broker.equity:.2f}"
                                        )

                pos = self.broker.positions[symbol]
                u_pct = self._unrealized_pct(pos.entry_price, last_close)
                if not is_long:
                    u_pct = -u_pct  # favorable move for a short is price falling
                trail_state = (
                    f"trail ext={pos.peak_price:.4f} stop={pos.stop_price:.4f}"
                    if pos.trail_armed
                    else "trail off"
                )
                return (
                    f"{symbol} in_position({direction}) legs={len(pos.legs)}/{self.settings.max_dca_legs} "
                    f"avg={pos.entry_price:.4f} last={last_close:.4f} ({u_pct:+.2f}%) {trail_state}"
                )

        # --- 2. No position: maybe open the first leg ---
        if self.paused:
            return f"{symbol} paused_no_entry last={last_close:.4f}"

        if not price_in_zone(symbol, last_close, self.settings, direction):
            cap = self.settings.max_price_for(symbol)
            return f"{symbol} no_entry above_cap last={last_close:.4f} cap={cap:.2f}"

        if not entry_trigger(closed_candles, self.settings, direction):
            ref = local_extreme(closed_candles, self.settings.high_lookback_candles, direction)
            return f"{symbol} no_entry({direction}) no_setup last={last_close:.4f} ref={ref:.4f}"

        if not in_trend(closed_candles, self.settings, direction):
            return f"{symbol} no_entry({direction}) trend_filter last={last_close:.4f}"

        if not self.risk.can_trade(self.broker.equity):
            print(f"[{self._utc_now()}] HALT risk guard active, no new trades {symbol}")
            return f"{symbol} blocked_by_risk last={last_close:.4f}"

        leg_size = self.risk.calc_leg_size(self.starting_equity, last_close)
        if leg_size <= 0:
            return f"{symbol} no_entry size=0 last={last_close:.4f}"

        pos = self.broker.open_leg(symbol, side, leg_size, last_close)
        if pos:
            pos.entry_candle_ts = current_candle_ts
            print(
                f"[{self._utc_now()}] OPEN {symbol} ({direction}) leg=1/{self.settings.max_dca_legs} "
                f"size={leg_size:.6f} px={last_close:.4f} equity={self.broker.equity:.2f}"
            )
            return (
                f"{symbol} opened({direction}) legs=1/{self.settings.max_dca_legs} "
                f"avg={pos.entry_price:.4f} last={last_close:.4f}"
            )
        return f"{symbol} no_entry broker_rejected last={last_close:.4f}"

    def _process_symbol_trend(self, symbol: str) -> str:
        """Trend-following path (STRATEGY=trend): single ATR-stopped position."""
        ohlcv = self.exchange.fetch_ohlcv(
            symbol,
            self.settings.timeframe,
            self.settings.lookback_candles + 1,
        )
        if len(ohlcv) < 2:
            return f"{symbol} insufficient_data"

        closed_candles = ohlcv[:-1]
        current_candle = ohlcv[-1]
        current_candle_ts = datetime.fromtimestamp(
            current_candle[0] / 1000, tz=timezone.utc
        )
        current_high = float(current_candle[2])
        current_low = float(current_candle[3])
        last_close = float(closed_candles[-1][4])

        direction = self.settings.direction_for(symbol)
        is_long = direction != "short"
        side = "buy" if is_long else "sell"
        atr = atr_value(closed_candles, self.settings)

        # --- 1. Manage open position (chandelier trail / regime exit) ---
        if symbol in self.broker.positions:
            pos = self.broker.positions[symbol]

            # Ratchet the favorable extreme and tighten the chandelier stop.
            if is_long:
                pos.peak_price = max(pos.peak_price, current_high)
            else:
                pos.peak_price = min(pos.peak_price, current_low)
            chand = chandelier_stop(pos.peak_price, atr, self.settings, direction)
            if chand > 0:
                if is_long:
                    pos.stop_price = max(pos.stop_price, chand)
                else:
                    pos.stop_price = chand if pos.stop_price <= 0 else min(pos.stop_price, chand)

            stop_hit = (
                pos.stop_price > 0
                and (current_low <= pos.stop_price if is_long else current_high >= pos.stop_price)
            )
            if stop_hit:
                trade = self.broker.close_all_legs(symbol, pos.stop_price, "trail")
                if trade:
                    self.risk.on_trade_close(trade.pnl, self.broker.equity)
                    print(
                        f"[{self._utc_now()}] EXIT {symbol} trail pnl={trade.pnl:+.2f} "
                        f"avg={trade.entry_price:.4f} exit={trade.exit_price:.4f} "
                        f"equity={self.broker.equity:.2f}"
                    )
                    return f"{symbol} exited trail pnl={trade.pnl:+.2f}"

            # Soft exit: trend (EMA cross) flipped against the position.
            if symbol in self.broker.positions and not regime_intact(closed_candles, self.settings, direction):
                trade = self.broker.close_all_legs(symbol, last_close, "regime")
                if trade:
                    self.risk.on_trade_close(trade.pnl, self.broker.equity)
                    print(
                        f"[{self._utc_now()}] EXIT {symbol} regime pnl={trade.pnl:+.2f} "
                        f"avg={trade.entry_price:.4f} exit={trade.exit_price:.4f} "
                        f"equity={self.broker.equity:.2f}"
                    )
                    return f"{symbol} exited regime pnl={trade.pnl:+.2f}"

            pos = self.broker.positions[symbol]
            u_pct = self._unrealized_pct(pos.entry_price, last_close)
            if not is_long:
                u_pct = -u_pct
            return (
                f"{symbol} in_position({direction}) avg={pos.entry_price:.4f} "
                f"last={last_close:.4f} ({u_pct:+.2f}%) stop={pos.stop_price:.4f}"
            )

        # --- 2. No position: enter on a fresh trend signal ---
        if self.paused:
            return f"{symbol} paused_no_entry last={last_close:.4f}"
        if atr <= 0:
            return f"{symbol} no_entry no_atr last={last_close:.4f}"
        if not trend_signal(closed_candles, self.settings, direction):
            return f"{symbol} no_entry({direction}) no_trend last={last_close:.4f}"
        if not self.risk.can_trade(self.broker.equity):
            print(f"[{self._utc_now()}] HALT risk guard active, no new trades {symbol}")
            return f"{symbol} blocked_by_risk last={last_close:.4f}"

        init_stop = initial_stop(last_close, atr, self.settings, direction)
        size = position_size(self.broker.equity, last_close, init_stop, self.settings)
        if size <= 0:
            return f"{symbol} no_entry size=0 last={last_close:.4f}"

        pos = self.broker.open_leg(symbol, side, size, last_close)
        if pos:
            pos.entry_candle_ts = current_candle_ts
            pos.stop_price = init_stop
            pos.peak_price = pos.entry_price
            print(
                f"[{self._utc_now()}] OPEN {symbol} ({direction}) trend "
                f"size={size:.6f} px={last_close:.4f} stop={init_stop:.4f} "
                f"atr={atr:.4f} equity={self.broker.equity:.2f}"
            )
            return f"{symbol} opened({direction}) avg={pos.entry_price:.4f} stop={init_stop:.4f}"
        return f"{symbol} no_entry broker_rejected last={last_close:.4f}"

    def run_forever(self) -> None:
        """Main loop: process all symbols, sleep, log heartbeats."""
        print(
            f"[{self._utc_now()}] Start bot mode={self.settings.mode} "
            f"symbols={self.settings.symbols} strategy=dca-on-dips"
        )
        print(
            f"  caps: {self.settings.long_max_prices} "
            f"initial_dip={self.settings.initial_dip_pct}% "
            f"dca_trigger={self.settings.dca_trigger_pct}% "
            f"max_legs={self.settings.max_dca_legs} "
            f"leg_notional={self.settings.leg_notional_pct}% "
            f"trail_arm={self.settings.trail_activate_pct}% "
            f"trail_dist={self.settings.trail_distance_pct}%"
        )
        while True:
            try:
                self.loop_count += 1
                # Refresh the cross-process pause flag once per tick (cheap file read).
                prev_paused = self.paused
                self.paused = control.is_paused(self.settings.logs_dir)
                if self.paused != prev_paused:
                    ctrl = control.read_control(self.settings.logs_dir)
                    print(
                        f"[{self._utc_now()}] "
                        f"{'PAUSED' if self.paused else 'RESUMED'} "
                        f"by={ctrl.get('by') or '?'} reason={ctrl.get('reason') or '-'}"
                    )
                    self.broker.log_event(
                        "trading_paused" if self.paused else "trading_resumed",
                        {"by": ctrl.get("by", ""), "reason": ctrl.get("reason", "")},
                    )
                statuses = []
                for symbol in self.settings.symbols:
                    statuses.append(self._process_symbol(symbol))
                if self.loop_count % self.settings.heartbeat_interval == 0:
                    print(
                        f"[{self._utc_now()}] HEARTBEAT loop={self.loop_count} "
                        f"equity={self.broker.equity:.2f} "
                        f"open_positions={len(self.broker.positions)}"
                        f"{' [PAUSED]' if self.paused else ''}"
                    )
                    for status in statuses:
                        print(f"  - {status}")
                    self.broker.log_event(
                        "heartbeat",
                        {
                            "loop": self.loop_count,
                            "equity": self.broker.equity,
                            "open_positions": len(self.broker.positions),
                            "paused": self.paused,
                            "positions": self._positions_snapshot(),
                            "statuses": statuses,
                        },
                    )
                time.sleep(self.settings.poll_seconds)
            except KeyboardInterrupt:
                print("Stopped by user.")
                break
            except Exception as exc:  # noqa: BLE001
                print(f"[{self._utc_now()}] ERROR {exc}")
                self.broker.log_event(
                    "error",
                    {
                        "loop": self.loop_count,
                        "message": str(exc),
                        "type": type(exc).__name__,
                    },
                )
                time.sleep(max(3, self.settings.poll_seconds // 2))
