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

from .config import Settings
from .exchange import ExchangeAdapter, LiveBroker, PaperBroker
from .risk import RiskManager
from .strategy import (
    dca_trigger,
    entry_trigger,
    local_high,
    price_in_zone,
    should_arm_trail,
    trail_stop_price,
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

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _unrealized_pct(self, avg_entry: float, last_close: float) -> float:
        if avg_entry <= 0:
            return 0.0
        return (last_close / avg_entry - 1.0) * 100.0

    def _process_symbol(self, symbol: str) -> str:
        """Process one symbol per tick.

        Returns a short status string for heartbeat logging.
        """
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

        # --- 1. Manage open position (trail / exit / DCA) ---
        if symbol in self.broker.positions:
            pos = self.broker.positions[symbol]

            if not pos.trail_armed and should_arm_trail(pos.entry_price, last_close, self.settings):
                pos.trail_armed = True
                pos.peak_price = max(pos.peak_price, current_high, last_close)
                pos.stop_price = trail_stop_price(pos.peak_price, self.settings)
                self.broker.log_event(
                    "trail_armed",
                    {
                        "symbol": symbol,
                        "avg_entry_price": pos.entry_price,
                        "last_close": last_close,
                        "peak_price": pos.peak_price,
                        "stop_price": pos.stop_price,
                    },
                )
                print(
                    f"[{self._utc_now()}] TRAIL ARMED {symbol} avg={pos.entry_price:.2f} "
                    f"peak={pos.peak_price:.2f} stop={pos.stop_price:.2f}"
                )

            if pos.trail_armed:
                if current_high > pos.peak_price:
                    pos.peak_price = current_high
                    pos.stop_price = trail_stop_price(pos.peak_price, self.settings)
                # exit if intrabar low touches trail
                if current_low <= pos.stop_price:
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

            # if still open, consider DCA add
            if symbol in self.broker.positions:
                pos = self.broker.positions[symbol]
                if len(pos.legs) < self.settings.max_dca_legs:
                    if dca_trigger(last_close, pos.last_fill_price, self.settings):
                        if price_in_zone(symbol, last_close, self.settings):
                            if self.risk.can_trade(self.broker.equity):
                                add_size = self.risk.calc_leg_size(
                                    self.starting_equity, last_close
                                )
                                if add_size > 0:
                                    new_pos = self.broker.open_leg(
                                        symbol, "buy", add_size, last_close
                                    )
                                    if new_pos:
                                        u_pct = self._unrealized_pct(new_pos.entry_price, last_close)
                                        print(
                                            f"[{self._utc_now()}] DCA ADD {symbol} leg={len(new_pos.legs)}/{self.settings.max_dca_legs} "
                                            f"size={add_size:.6f} px={last_close:.2f} "
                                            f"avg={new_pos.entry_price:.2f} ({u_pct:+.2f}%) equity={self.broker.equity:.2f}"
                                        )

                pos = self.broker.positions[symbol]
                u_pct = self._unrealized_pct(pos.entry_price, last_close)
                trail_state = (
                    f"trail peak={pos.peak_price:.2f} stop={pos.stop_price:.2f}"
                    if pos.trail_armed
                    else f"trail off (need {pos.entry_price * (1 + self.settings.trail_activate_pct / 100):.2f})"
                )
                return (
                    f"{symbol} in_position legs={len(pos.legs)}/{self.settings.max_dca_legs} "
                    f"avg={pos.entry_price:.2f} last={last_close:.2f} ({u_pct:+.2f}%) {trail_state}"
                )

        # --- 2. No position: maybe open the first leg ---
        if not price_in_zone(symbol, last_close, self.settings):
            cap = self.settings.max_price_for(symbol)
            return f"{symbol} no_entry above_cap last={last_close:.2f} cap={cap:.2f}"

        if not entry_trigger(closed_candles, self.settings):
            high = local_high(closed_candles, self.settings.high_lookback_candles)
            need = high * (1.0 - self.settings.initial_dip_pct / 100.0)
            return (
                f"{symbol} no_entry no_dip last={last_close:.2f} "
                f"high={high:.2f} need<={need:.2f}"
            )

        if not self.risk.can_trade(self.broker.equity):
            print(f"[{self._utc_now()}] HALT risk guard active, no new trades {symbol}")
            return f"{symbol} blocked_by_risk last={last_close:.2f}"

        leg_size = self.risk.calc_leg_size(self.starting_equity, last_close)
        if leg_size <= 0:
            return f"{symbol} no_entry size=0 last={last_close:.2f}"

        pos = self.broker.open_leg(symbol, "buy", leg_size, last_close)
        if pos:
            pos.entry_candle_ts = current_candle_ts
            print(
                f"[{self._utc_now()}] OPEN {symbol} leg=1/{self.settings.max_dca_legs} "
                f"size={leg_size:.6f} px={last_close:.2f} equity={self.broker.equity:.2f}"
            )
            return (
                f"{symbol} opened legs=1/{self.settings.max_dca_legs} "
                f"avg={pos.entry_price:.2f} last={last_close:.2f}"
            )
        return f"{symbol} no_entry broker_rejected last={last_close:.2f}"

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
                statuses = []
                for symbol in self.settings.symbols:
                    statuses.append(self._process_symbol(symbol))
                if self.loop_count % self.settings.heartbeat_interval == 0:
                    print(
                        f"[{self._utc_now()}] HEARTBEAT loop={self.loop_count} "
                        f"equity={self.broker.equity:.2f} "
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
                self.broker.log_event(
                    "error",
                    {
                        "loop": self.loop_count,
                        "message": str(exc),
                        "type": type(exc).__name__,
                    },
                )
                time.sleep(max(3, self.settings.poll_seconds // 2))
