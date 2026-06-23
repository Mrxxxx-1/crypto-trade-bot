"""Offline backtest of the DCA-on-dips strategy on cached OHLCV.

Lifecycle per symbol mirrors ``src/bot.py``:
  1. Open leg 1 when price <= cap AND last_close <= local_high * (1 - initial_dip_pct/100).
  2. Add legs (up to ``max_dca_legs``) when last_close <= last_fill * (1 - dca_trigger_pct/100).
  3. Arm trailing once last_close >= avg_entry * (1 + trail_activate_pct/100).
  4. Once armed, ratchet peak to running high; exit ALL legs at trail level when low touches it.

Usage:
    python -m src.fetch_candles                       # download data first
    python -m src.backtest                            # baseline run
    python -m src.backtest --set DCA_TRIGGER_PCT=10   # tweak a knob
    python -m src.backtest --label TIGHTER_TRAIL --set TRAIL_DISTANCE_PCT=2
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

import bisect

from .config import Settings, load_settings
from .exchange import _timeframe_ms
from .strategy import (
    adx_ok,
    dca_trigger,
    entry_trigger,
    htf_trend_ok,
    in_trend,
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


# ---------------------------------------------------------------------------
# Backtest data structures
# ---------------------------------------------------------------------------

@dataclass
class BTLeg:
    size: float
    entry_price: float
    opened_at: datetime
    entry_candle: int
    fee: float = 0.0


@dataclass
class BTPosition:
    symbol: str
    side: str
    size: float
    entry_price: float
    opened_at: datetime
    entry_candle: int
    last_fill_price: float = 0.0
    legs: List[BTLeg] = field(default_factory=list)
    trail_armed: bool = False
    peak_price: float = 0.0
    stop_price: float = 0.0


@dataclass
class BTTrade:
    symbol: str
    side: str
    size: float
    entry_price: float
    exit_price: float
    pnl: float
    fees: float
    opened_at: datetime
    closed_at: datetime
    exit_reason: str
    legs: int = 1


@dataclass
class BTResult:
    trades: List[BTTrade]
    equity_curve: List[Tuple[datetime, float]]
    starting_equity: float
    final_equity: float


# ---------------------------------------------------------------------------
# Risk tracker (mirrors RiskManager but uses candle timestamps)
# ---------------------------------------------------------------------------

class BacktestRisk:
    """Backtest-side risk manager; uses candle timestamps for halt timers
    instead of wall-clock time (mirrors ``RiskManager`` semantics).
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.window_start_equity: float = settings.initial_equity
        self.consecutive_losses: int = 0
        self.halted_until: Optional[datetime] = None

    def _check_halt_expired(self, candle_time: datetime, equity: float) -> None:
        if self.halted_until is not None and candle_time >= self.halted_until:
            self.halted_until = None
            self.consecutive_losses = 0
            self.window_start_equity = equity

    def can_trade(self, candle_time: datetime, equity: float) -> bool:
        self._check_halt_expired(candle_time, equity)

        if self.halted_until is not None:
            return False

        dd_pct = ((self.window_start_equity - equity) / max(self.window_start_equity, 1e-9)) * 100
        if dd_pct >= self.settings.max_daily_loss_pct:
            self.halted_until = candle_time + timedelta(hours=self.settings.daily_loss_halt_hours)
            return False

        if self.consecutive_losses >= self.settings.max_consecutive_losses:
            self.halted_until = candle_time + timedelta(hours=self.settings.consec_halt_hours)
            return False

        return True

    def on_trade_close(self, candle_time: datetime, pnl: float, equity: float) -> None:
        self._check_halt_expired(candle_time, equity)
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def calc_leg_size(self, starting_equity: float, current_price: float) -> float:
        if current_price <= 0 or starting_equity <= 0:
            return 0.0
        notional = starting_equity * (self.settings.leg_notional_pct / 100.0)
        size = notional / current_price
        max_size = (starting_equity * self.settings.max_leverage) / current_price
        return max(0.0, min(size, max_size))


# ---------------------------------------------------------------------------
# Price helpers (match PaperBroker logic)
# ---------------------------------------------------------------------------

def _exec_price(price: float, side: str, slippage_bps: float) -> float:
    slip = slippage_bps / 10_000
    return price * (1 + slip) if side == "buy" else price * (1 - slip)


def _fee(notional: float, fee_bps: float) -> float:
    return notional * (fee_bps / 10_000)


def _recompute(pos: BTPosition) -> None:
    """Refresh ``size`` and ``entry_price`` (size-weighted avg) from legs."""
    total = sum(l.size for l in pos.legs)
    if total <= 0:
        pos.size = 0.0
        pos.entry_price = 0.0
        return
    pos.size = total
    pos.entry_price = sum(l.size * l.entry_price for l in pos.legs) / total


def _close_bt_position(
    positions: Dict[str, BTPosition],
    trades: List["BTTrade"],
    symbol: str,
    pos: BTPosition,
    exit_at: float,
    reason: str,
    settings: Settings,
    candle_ts,
) -> float:
    """Close all legs at ``exit_at``, record the trade, and return realized net P&L.

    Direction-aware: a short (``side == "sell"``) profits when the exit price is
    below the average entry, so its raw P&L is negated.
    """
    exit_side = "sell" if pos.side == "buy" else "buy"
    exit_px = _exec_price(exit_at, exit_side, settings.slippage_bps)
    total_size = pos.size
    exit_fee = _fee(exit_px * total_size, settings.exit_fee_bps)
    raw_pnl = (exit_px - pos.entry_price) * total_size
    if pos.side == "sell":
        raw_pnl = -raw_pnl
    net_pnl = raw_pnl - exit_fee
    trades.append(
        BTTrade(
            symbol=symbol,
            side=pos.side,
            size=total_size,
            entry_price=pos.entry_price,
            exit_price=exit_px,
            pnl=net_pnl,
            fees=exit_fee + sum(l.fee for l in pos.legs),
            opened_at=pos.opened_at,
            closed_at=candle_ts,
            exit_reason=reason,
            legs=len(pos.legs),
        )
    )
    del positions[symbol]
    return net_pnl


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

def run_backtest(
    settings: Settings,
    candles_by_symbol: Dict[str, List[list]],
    htf_by_symbol: Optional[Dict[str, List[list]]] = None,
) -> BTResult:
    """Replay candles through the DCA strategy.

    ``htf_by_symbol`` (optional) holds higher-timeframe candles for the MTF
    filter; only consulted when ``MTF_ENABLED``.
    """
    min_len = min(len(v) for v in candles_by_symbol.values())
    for sym in list(candles_by_symbol):
        candles_by_symbol[sym] = candles_by_symbol[sym][-min_len:]

    # Higher-timeframe alignment data (MTF filter). Precompute each HTF candle's
    # *close* time so we only ever look at bars fully closed by the current bar.
    htf_by_symbol = htf_by_symbol or {}
    htf_interval_ms = _timeframe_ms(settings.mtf_timeframe)
    htf_close_times: Dict[str, List[int]] = {
        sym: [int(r[0]) + htf_interval_ms for r in rows]
        for sym, rows in htf_by_symbol.items()
    }

    def _htf_window(symbol: str, cur_open_ms: int) -> List[list]:
        rows = htf_by_symbol.get(symbol)
        if not rows:
            return []
        n = bisect.bisect_right(htf_close_times[symbol], cur_open_ms)
        start = max(0, n - settings.lookback_candles)
        return rows[start:n]

    equity = settings.initial_equity
    starting_equity = settings.initial_equity
    positions: Dict[str, BTPosition] = {}
    trades: List[BTTrade] = []
    risk = BacktestRisk(settings)
    lookback = max(settings.lookback_candles, settings.high_lookback_candles + 1)
    if lookback >= min_len:
        raise ValueError(
            f"Not enough candles: have {min_len}, need > {lookback} (lookback + 1)."
        )

    ref_symbol = settings.symbols[0]
    equity_curve: List[Tuple[datetime, float]] = [
        (
            datetime.fromtimestamp(candles_by_symbol[ref_symbol][lookback][0] / 1000, tz=timezone.utc),
            equity,
        )
    ]

    for i in range(lookback, min_len):
        candle_ts = datetime.fromtimestamp(
            candles_by_symbol[ref_symbol][i][0] / 1000, tz=timezone.utc
        )

        for symbol in settings.symbols:
            candle = candles_by_symbol[symbol][i]
            high = float(candle[2])
            low = float(candle[3])
            close = float(candle[4])

            direction = settings.direction_for(symbol)
            is_long = direction != "short"
            side = "buy" if is_long else "sell"

            # ===== Trend-following strategy path =====
            if settings.strategy == "trend":
                closed_window = candles_by_symbol[symbol][i - lookback : i]
                atr = atr_value(closed_window, settings)

                if symbol in positions:
                    pos = positions[symbol]
                    first_leg_candle = pos.legs[0].entry_candle if pos.legs else i
                    allow_exit = i > first_leg_candle

                    if is_long:
                        pos.peak_price = max(pos.peak_price, high)
                    else:
                        pos.peak_price = min(pos.peak_price, low)
                    chand = chandelier_stop(pos.peak_price, atr, settings, direction)
                    if chand > 0:
                        if is_long:
                            pos.stop_price = max(pos.stop_price, chand)
                        else:
                            pos.stop_price = chand if pos.stop_price <= 0 else min(pos.stop_price, chand)

                    stop_hit = pos.stop_price > 0 and (
                        low <= pos.stop_price if is_long else high >= pos.stop_price
                    )
                    if allow_exit and stop_hit:
                        net_pnl = _close_bt_position(
                            positions, trades, symbol, pos, pos.stop_price, "trail", settings, candle_ts
                        )
                        equity += net_pnl
                        risk.on_trade_close(candle_ts, net_pnl, equity)
                        continue

                    if allow_exit and not regime_intact(closed_window, settings, direction):
                        net_pnl = _close_bt_position(
                            positions, trades, symbol, pos, close, "regime", settings, candle_ts
                        )
                        equity += net_pnl
                        risk.on_trade_close(candle_ts, net_pnl, equity)
                    continue

                # No position: enter on a fresh trend signal
                if atr <= 0:
                    continue
                if not trend_signal(closed_window, settings, direction):
                    continue
                if not adx_ok(closed_window, settings):
                    continue
                if not volume_ok(closed_window, settings):
                    continue
                if not htf_trend_ok(_htf_window(symbol, int(candle[0])), settings, direction):
                    continue
                if not risk.can_trade(candle_ts, equity):
                    continue
                init_stop = initial_stop(close, atr, settings, direction)
                size = position_size(equity, close, init_stop, settings)
                if size <= 0:
                    continue
                entry_px = _exec_price(close, side, settings.slippage_bps)
                entry_fee = _fee(entry_px * size, settings.entry_fee_bps)
                equity -= entry_fee
                positions[symbol] = BTPosition(
                    symbol=symbol,
                    side=side,
                    size=size,
                    entry_price=entry_px,
                    opened_at=candle_ts,
                    entry_candle=i,
                    last_fill_price=entry_px,
                    peak_price=entry_px,
                    stop_price=init_stop,
                    legs=[
                        BTLeg(
                            size=size,
                            entry_price=entry_px,
                            opened_at=candle_ts,
                            entry_candle=i,
                            fee=entry_fee,
                        )
                    ],
                )
                continue

            # Optional ATR chandelier trail for DCA exits (else percent trail).
            dca_atr = (
                atr_value(candles_by_symbol[symbol][i - lookback : i], settings)
                if settings.dca_chandelier_enabled
                else 0.0
            )

            def _dca_trail_stop(extreme: float, _atr: float = dca_atr, _dir: str = direction) -> float:
                if settings.dca_chandelier_enabled and _atr > 0:
                    return chandelier_stop(extreme, _atr, settings, _dir)
                return trail_stop_price(extreme, settings, _dir)

            # --- 1. Manage open position (TP / trail / stop / DCA) ---
            if symbol in positions:
                pos = positions[symbol]

                # Skip *exit* checks on the entry candle for the very first leg
                # (matches live bot behaviour and prevents same-bar entry+exit).
                first_leg_candle = pos.legs[0].entry_candle if pos.legs else i
                allow_exit = i > first_leg_candle

                # Fixed take-profit: exit all legs once the bar reaches the target.
                tp_price = take_profit_price(pos.entry_price, settings, direction)
                tp_hit = tp_price > 0 and (high >= tp_price if is_long else low <= tp_price)
                if allow_exit and tp_hit:
                    net_pnl = _close_bt_position(
                        positions, trades, symbol, pos, tp_price, "tp", settings, candle_ts
                    )
                    equity += net_pnl
                    risk.on_trade_close(candle_ts, net_pnl, equity)
                    continue

                # Arm trail once price has moved favorably by trail_activate_pct.
                if not pos.trail_armed and should_arm_trail(pos.entry_price, close, settings, direction):
                    pos.trail_armed = True
                    pos.peak_price = (
                        max(pos.peak_price, high, close)
                        if is_long
                        else min(pos.peak_price, low, close)
                    )
                    pos.stop_price = _dca_trail_stop(pos.peak_price)

                if pos.trail_armed:
                    if is_long and high > pos.peak_price:
                        pos.peak_price = high
                        pos.stop_price = _dca_trail_stop(pos.peak_price)
                    elif not is_long and low < pos.peak_price:
                        pos.peak_price = low
                        pos.stop_price = _dca_trail_stop(pos.peak_price)

                    trail_hit = low <= pos.stop_price if is_long else high >= pos.stop_price
                    if allow_exit and trail_hit:
                        net_pnl = _close_bt_position(
                            positions, trades, symbol, pos, pos.stop_price, "trail", settings, candle_ts
                        )
                        equity += net_pnl
                        risk.on_trade_close(candle_ts, net_pnl, equity)
                        continue

                # Hard stop-loss: cap catastrophic adverse moves (checked when not
                # exited by trail). Exits all legs at the stop price.
                sl_price = stop_loss_price(pos.entry_price, settings, direction)
                sl_hit = sl_price > 0 and (low <= sl_price if is_long else high >= sl_price)
                if allow_exit and sl_hit:
                    net_pnl = _close_bt_position(
                        positions, trades, symbol, pos, sl_price, "stop", settings, candle_ts
                    )
                    equity += net_pnl
                    risk.on_trade_close(candle_ts, net_pnl, equity)
                    continue

                # Maybe add a DCA leg (further in the adverse direction)
                if symbol in positions:
                    pos = positions[symbol]
                    if len(pos.legs) < settings.max_dca_legs:
                        window = candles_by_symbol[symbol][i - lookback : i + 1]
                        closed_window = window[:-1]
                        if dca_trigger(close, pos.last_fill_price, settings, direction) and in_trend(closed_window, settings, direction):
                            if price_in_zone(symbol, close, settings, direction) and risk.can_trade(candle_ts, equity):
                                add_size = risk.calc_leg_size(starting_equity, close)
                                if add_size > 0:
                                    entry_px = _exec_price(close, side, settings.slippage_bps)
                                    entry_fee = _fee(entry_px * add_size, settings.entry_fee_bps)
                                    equity -= entry_fee
                                    pos.legs.append(
                                        BTLeg(
                                            size=add_size,
                                            entry_price=entry_px,
                                            opened_at=candle_ts,
                                            entry_candle=i,
                                            fee=entry_fee,
                                        )
                                    )
                                    pos.last_fill_price = entry_px
                                    _recompute(pos)

                # Still in position; on to next symbol
                continue

            # --- 2. No position: maybe open the first leg ---
            window = candles_by_symbol[symbol][i - lookback : i + 1]
            closed_window = window[:-1]  # exclude the bar we're acting on, mirror bot.py

            if not price_in_zone(symbol, close, settings, direction):
                continue
            if not entry_trigger(closed_window, settings, direction):
                continue
            if not in_trend(closed_window, settings, direction):
                continue
            if not volume_ok(closed_window, settings):
                continue
            if not htf_trend_ok(_htf_window(symbol, int(candle[0])), settings, direction):
                continue
            if not risk.can_trade(candle_ts, equity):
                continue

            leg_size = risk.calc_leg_size(starting_equity, close)
            if leg_size <= 0:
                continue

            entry_px = _exec_price(close, side, settings.slippage_bps)
            entry_fee = _fee(entry_px * leg_size, settings.entry_fee_bps)
            equity -= entry_fee

            positions[symbol] = BTPosition(
                symbol=symbol,
                side=side,
                size=leg_size,
                entry_price=entry_px,
                opened_at=candle_ts,
                entry_candle=i,
                last_fill_price=entry_px,
                peak_price=entry_px,
                legs=[
                    BTLeg(
                        size=leg_size,
                        entry_price=entry_px,
                        opened_at=candle_ts,
                        entry_candle=i,
                        fee=entry_fee,
                    )
                ],
            )

        equity_curve.append((candle_ts, equity))

    return BTResult(
        trades=trades,
        equity_curve=equity_curve,
        starting_equity=settings.initial_equity,
        final_equity=equity,
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(result: BTResult, label: str = "BACKTEST") -> None:
    trades = result.trades
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]

    net_pnl = result.final_equity - result.starting_equity
    net_pct = (net_pnl / result.starting_equity) * 100

    peak = result.starting_equity
    max_dd = 0.0
    for _, eq in result.equity_curve:
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
    max_dd_pct = (max_dd / result.starting_equity) * 100

    gross_win = sum(t.pnl for t in wins) if wins else 0.0
    gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0.0
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

    max_consec = cur_consec = 0
    for t in trades:
        if t.pnl < 0:
            cur_consec += 1
            max_consec = max(max_consec, cur_consec)
        else:
            cur_consec = 0

    total_fees = sum(t.fees for t in trades)
    n = max(len(trades), 1)
    avg_legs = sum(t.legs for t in trades) / n if trades else 0.0
    max_legs = max((t.legs for t in trades), default=0)
    trail_exits = sum(1 for t in trades if t.exit_reason == "trail")

    first_date = result.equity_curve[0][0].strftime("%Y-%m-%d") if result.equity_curve else "?"
    last_date = result.equity_curve[-1][0].strftime("%Y-%m-%d") if result.equity_curve else "?"

    w = 52
    print(f"\n{'=' * w}")
    print(f"  {label}")
    print(f"  Period: {first_date}  to  {last_date}")
    print(f"{'=' * w}")
    print(f"  Starting equity:     ${result.starting_equity:>10,.2f}")
    print(f"  Final equity:        ${result.final_equity:>10,.2f}")
    print(f"  Net P&L:             ${net_pnl:>+10,.2f}  ({net_pct:+.1f}%)")
    print(f"  Max drawdown:        ${max_dd:>10,.2f}  ({max_dd_pct:.1f}%)")
    print(f"  Total fees:          ${total_fees:>10,.2f}")
    print(f"{'-' * w}")
    print(f"  Total trades:        {len(trades):>6}")
    print(f"  Win rate:            {len(wins)}/{len(trades)}  ({len(wins)/n*100:.1f}%)")
    print(f"  Avg win:             ${(gross_win / max(len(wins), 1)):>10,.2f}")
    print(f"  Avg loss:            ${-(gross_loss / max(len(losses), 1)):>10,.2f}")
    print(f"  Profit factor:       {profit_factor:>10.2f}")
    print(f"{'-' * w}")
    print(f"  Trail exits:         {trail_exits:>6}")
    print(f"  Avg legs/trade:      {avg_legs:>6.2f}")
    print(f"  Max legs in a trade: {max_legs:>6}")
    print(f"  Max consec losses:   {max_consec:>6}")
    print(f"  Avg trade P&L:       ${net_pnl / n:>+10,.2f}")
    print(f"{'-' * w}")

    monthly: dict[str, list[BTTrade]] = {}
    for t in trades:
        key = t.closed_at.strftime("%Y-%m")
        monthly.setdefault(key, []).append(t)
    if monthly:
        print(f"  {'Month':<10} {'Trades':>6} {'WinR':>6} {'P&L':>10} {'PF':>6}")
        for month in sorted(monthly):
            mt = monthly[month]
            mw = [t for t in mt if t.pnl > 0]
            ml = [t for t in mt if t.pnl <= 0]
            m_pnl = sum(t.pnl for t in mt)
            m_gw = sum(t.pnl for t in mw)
            m_gl = abs(sum(t.pnl for t in ml))
            m_pf = m_gw / m_gl if m_gl > 0 else float("inf")
            wr = len(mw) / len(mt) * 100
            print(f"  {month:<10} {len(mt):>6} {wr:>5.0f}% ${m_pnl:>+9,.0f} {m_pf:>5.2f}")

    print(f"{'=' * w}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_SETTINGS_FLOATS = {
    "INITIAL_EQUITY", "MAX_LEVERAGE", "MAX_DAILY_LOSS_PCT",
    "INITIAL_DIP_PCT", "DCA_TRIGGER_PCT", "LEG_NOTIONAL_PCT",
    "TRAIL_ACTIVATE_PCT", "TRAIL_DISTANCE_PCT", "STOP_LOSS_PCT", "TAKE_PROFIT_PCT",
    "ENTRY_FEE_BPS", "EXIT_FEE_BPS", "SLIPPAGE_BPS",
    "CONSEC_HALT_HOURS", "DAILY_LOSS_HALT_HOURS",
    "RISK_PER_TRADE_PCT", "STOP_ATR_MULTIPLIER", "TRAIL_ATR_MULTIPLIER",
    "ADX_MIN", "VOLUME_MIN_MULT",
}
_SETTINGS_INTS = {
    "POLL_SECONDS", "LOOKBACK_CANDLES", "MAX_CONSECUTIVE_LOSSES",
    "HIGH_LOOKBACK_CANDLES", "DIP_MEMORY_BARS", "MAX_DCA_LEGS",
    "HEARTBEAT_INTERVAL", "LIMIT_TIMEOUT_SECONDS", "TREND_EMA_PERIOD",
    "FAST_EMA", "SLOW_EMA", "ATR_PERIOD",
    "ADX_PERIOD", "VOLUME_MA_PERIOD", "MTF_EMA_PERIOD",
}
_SETTINGS_BOOLS = {
    "REQUIRE_GREEN_CONFIRMATION", "TREND_FILTER_ENABLED",
    "MTF_ENABLED", "DCA_CHANDELIER_ENABLED",
}
# String keys (e.g. MTF_TIMEFRAME) fall through to the default str handling.


def _apply_overrides(settings: Settings, overrides: list[str]) -> Settings:
    """Return a new Settings with --set KEY=VALUE overrides applied."""
    kw: dict = {}
    for item in overrides:
        if "=" not in item:
            print(f"WARNING: ignoring malformed override '{item}' (expected KEY=VALUE)")
            continue
        key, val = item.split("=", 1)
        field_name = key.lower()
        if key in _SETTINGS_FLOATS:
            kw[field_name] = float(val)
        elif key in _SETTINGS_INTS:
            kw[field_name] = int(val)
        elif key in _SETTINGS_BOOLS:
            kw[field_name] = val.strip().lower() in ("1", "true", "yes")
        else:
            kw[field_name] = val
    return replace(settings, **kw) if kw else settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the DCA-on-dips strategy on cached candle data")
    parser.add_argument("--env", default=".env.example", help="Env file to load settings from")
    parser.add_argument("--data-dir", default="data", help="Directory with cached candle JSON files")
    parser.add_argument("--label", default="BACKTEST", help="Label for the report header")
    parser.add_argument("--set", nargs="*", default=[], dest="overrides",
                        help="Override settings, e.g. --set DCA_TRIGGER_PCT=10 TRAIL_DISTANCE_PCT=2")
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    settings = load_settings()
    settings = _apply_overrides(settings, args.overrides)

    if args.overrides:
        print(f"Overrides applied: {', '.join(args.overrides)}")

    data_dir = Path(args.data_dir)
    candles_by_symbol: Dict[str, list] = {}
    for symbol in settings.symbols:
        safe = symbol.replace("/", "-").replace(":", "-")
        path = data_dir / f"{safe}_{settings.timeframe}.json"
        if not path.exists():
            print(f"ERROR: No cached data at {path}")
            print("Run first:  python -m src.fetch_candles")
            sys.exit(1)
        with path.open(encoding="utf-8") as f:
            candles_by_symbol[symbol] = json.load(f)
        print(f"Loaded {len(candles_by_symbol[symbol]):,} candles for {symbol}")

    # Higher-timeframe data for the MTF filter (only when enabled).
    htf_by_symbol: Dict[str, list] = {}
    if settings.mtf_enabled:
        for symbol in settings.symbols:
            safe = symbol.replace("/", "-").replace(":", "-")
            htf_path = data_dir / f"{safe}_{settings.mtf_timeframe}.json"
            if not htf_path.exists():
                print(f"ERROR: MTF_ENABLED but no HTF data at {htf_path}")
                print(f"Run first:  python -m src.fetch_candles --timeframes {settings.mtf_timeframe}")
                sys.exit(1)
            with htf_path.open(encoding="utf-8") as f:
                htf_by_symbol[symbol] = json.load(f)
            print(f"Loaded {len(htf_by_symbol[symbol]):,} HTF ({settings.mtf_timeframe}) candles for {symbol}")

    flags: list[str] = [
        f"caps={settings.long_max_prices}",
        f"initial_dip={settings.initial_dip_pct}%",
        f"high_lookback={settings.high_lookback_candles}",
        f"dca_trigger={settings.dca_trigger_pct}%",
        f"leg_notional={settings.leg_notional_pct}%",
        f"max_legs={settings.max_dca_legs}",
        f"trail_arm={settings.trail_activate_pct}%",
        f"trail_dist={settings.trail_distance_pct}%",
        f"consec_halt={settings.consec_halt_hours}h",
        f"daily_halt={settings.daily_loss_halt_hours}h",
    ]
    print(f"Flags: {', '.join(flags)}")

    result = run_backtest(settings, candles_by_symbol, htf_by_symbol)
    print_report(result, label=args.label)


if __name__ == "__main__":
    main()
