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

from . import control, hedge, hedge_broker, log_hygiene
from .config import Settings
from .exchange import ExchangeAdapter, LiveBroker, PaperBroker
from .risk import RiskManager
from .strategy import (
    adx_ok,
    dca_trigger,
    entry_trigger,
    htf_trend_ok,
    in_trend,
    local_extreme,
    price_in_zone,
    should_arm_trail,
    stop_loss_price,
    take_profit_price,
    trail_stop_price,
    volume_ok,
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
        # Heavy heartbeat fields are logged periodically; logs/state.json always
        # holds the latest full snapshot for the dashboard / MCP / Telegram layer.
        self._heartbeat_log = log_hygiene.HeartbeatThinner()
        # Cross-process pause flag (toggled by the Telegram/MCP agent layer).
        # Re-read once per loop in run_forever; only blocks NEW entries/adds.
        self.paused = control.is_paused(settings.logs_dir)
        # Manual catalyst hedge (opt-in). Built here so a misconfigured hedge
        # surfaces at startup, but never prevents the bot from trading.
        self.hedge: hedge_broker.HedgeManager | None = None
        if settings.hedge_enabled:
            try:
                self.hedge = hedge_broker.build_manager(settings, log_event=self.broker.log_event)
            except Exception as exc:  # noqa: BLE001
                print(f"[{self._utc_now()}] HEDGE disabled: {exc}")
                self.broker.log_event("hedge_init_failed", {"error": str(exc)})

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

    def _htf_closed(self, symbol: str) -> list:
        """Closed higher-timeframe candles for the MTF filter ([] when disabled)."""
        if not self.settings.mtf_enabled:
            return []
        htf = self.exchange.fetch_ohlcv(
            symbol,
            self.settings.mtf_timeframe,
            self.settings.lookback_candles + 1,
        )
        return htf[:-1] if len(htf) >= 2 else htf

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

        # Optional ATR chandelier trail for DCA exits (else percent trail).
        dca_atr = atr_value(closed_candles, self.settings) if self.settings.dca_chandelier_enabled else 0.0

        def _dca_trail_stop(extreme: float) -> float:
            if self.settings.dca_chandelier_enabled and dca_atr > 0:
                return chandelier_stop(extreme, dca_atr, self.settings, direction)
            return trail_stop_price(extreme, self.settings, direction)

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
                pos.stop_price = _dca_trail_stop(pos.peak_price)
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
                    pos.stop_price = _dca_trail_stop(pos.peak_price)
                elif not is_long and current_low < pos.peak_price:
                    pos.peak_price = current_low
                    pos.stop_price = _dca_trail_stop(pos.peak_price)
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

        if not volume_ok(closed_candles, self.settings):
            return f"{symbol} no_entry({direction}) low_volume last={last_close:.4f}"

        if not htf_trend_ok(self._htf_closed(symbol), self.settings, direction):
            return f"{symbol} no_entry({direction}) htf_misaligned last={last_close:.4f}"

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
        if not adx_ok(closed_candles, self.settings):
            return f"{symbol} no_entry({direction}) adx_chop last={last_close:.4f}"
        if not volume_ok(closed_candles, self.settings):
            return f"{symbol} no_entry({direction}) low_volume last={last_close:.4f}"
        if not htf_trend_ok(self._htf_closed(symbol), self.settings, direction):
            return f"{symbol} no_entry({direction}) htf_misaligned last={last_close:.4f}"
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

    def _hedge_candles(self, symbol: str) -> list:
        """Closed candles for hedge ATR sizing (drops the forming bar)."""
        ohlcv = self.exchange.fetch_ohlcv(
            symbol, self.settings.timeframe, self.settings.lookback_candles
        )
        return ohlcv[:-1] if len(ohlcv) > 1 else ohlcv

    def _poll_hedge(self) -> str:
        """Advance the manual hedge, if one is armed. Never raises."""
        if self.hedge is None:
            return ""
        try:
            state = self.hedge.poll(self._hedge_candles, self.exchange.fetch_last_price)
        except Exception as exc:  # noqa: BLE001
            print(f"[{self._utc_now()}] HEDGE ERROR {exc}")
            self.broker.log_event("hedge_error", {"error": str(exc)})
            return "hedge error"
        if state is None:
            return ""
        return state.summary()

    def run_forever(self) -> None:
        """Main loop: process all symbols, sleep, log heartbeats."""
        print(
            f"[{self._utc_now()}] Start bot mode={self.settings.mode} "
            f"symbols={self.settings.symbols} strategy={self.settings.strategy} "
            f"timeframe={self.settings.timeframe}"
        )
        if self.settings.strategy == "trend":
            print(
                f"  trend: fast_ema={self.settings.fast_ema} slow_ema={self.settings.slow_ema} "
                f"regime_ema={self.settings.trend_ema_period} atr={self.settings.atr_period} "
                f"stop_atr={self.settings.stop_atr_multiplier}x trail_atr={self.settings.trail_atr_multiplier}x "
                f"risk={self.settings.risk_per_trade_pct}% "
                f"adx_min={self.settings.adx_min} vol_mult={self.settings.volume_min_mult} "
                f"mtf={self.settings.mtf_enabled}"
            )
        else:
            print(
                f"  caps: {self.settings.long_max_prices} "
                f"initial_dip={self.settings.initial_dip_pct}% "
                f"dca_trigger={self.settings.dca_trigger_pct}% "
                f"max_legs={self.settings.max_dca_legs} "
                f"leg_notional={self.settings.leg_notional_pct}% "
                f"trail_arm={self.settings.trail_activate_pct}% "
                f"trail_dist={self.settings.trail_distance_pct}% "
                f"chandelier={self.settings.dca_chandelier_enabled}"
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
                hedge_status = self._poll_hedge()
                if hedge_status:
                    statuses.append(f"hedge: {hedge_status}")
                if self.loop_count % self.settings.heartbeat_interval == 0:
                    print(
                        f"[{self._utc_now()}] HEARTBEAT loop={self.loop_count} "
                        f"equity={self.broker.equity:.2f} "
                        f"open_positions={len(self.broker.positions)}"
                        f"{' [PAUSED]' if self.paused else ''}"
                    )
                    for status in statuses:
                        print(f"  - {status}")
                    snapshot = {
                        "loop": self.loop_count,
                        "equity": self.broker.equity,
                        "open_positions": len(self.broker.positions),
                        "paused": self.paused,
                        "positions": self._positions_snapshot(),
                        "statuses": statuses,
                    }
                    if self.settings.hedge_enabled:
                        active = hedge.read_hedge(self.settings.logs_dir)
                        snapshot["hedge"] = active.to_dict() if active else None
                    log_hygiene.write_state_snapshot(
                        self.settings.logs_dir,
                        {"ts": self._utc_now(), "event": "heartbeat", **snapshot},
                    )
                    self.broker.log_event("heartbeat", self._heartbeat_log.thin(snapshot))
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
