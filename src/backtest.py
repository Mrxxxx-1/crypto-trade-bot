"""Offline backtest: replay cached OHLCV candles through the strategy.

Usage:
    python -m src.fetch_candles                          # download data first
    python -m src.backtest                               # baseline run
    python -m src.backtest --set TAKE_PROFIT_R=1.0       # test a parameter tweak
    python -m src.backtest --cooldown 6                  # require 6-candle wait after exit
    python -m src.backtest --carry-losses                # don't reset loss streak at midnight
    python -m src.backtest --label IMPROVED --env .env.improved
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

from .config import Settings, load_settings
from .strategy import generate_signal, htf_trend, volume_ok


# ---------------------------------------------------------------------------
# Backtest data structures
# ---------------------------------------------------------------------------

@dataclass
class BTPosition:
    symbol: str
    side: str
    size: float
    entry_price: float
    stop_price: float
    take_profit_price: float
    opened_at: datetime
    entry_candle: int


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
    def __init__(self, settings: Settings, *, carry_losses: bool = False) -> None:
        self.settings = settings
        self.day: Optional[date] = None
        self.day_start_equity: float = settings.initial_equity
        self.consecutive_losses: int = 0
        self.halted: bool = False
        self.carry_losses = carry_losses

    def _roll_day(self, current_date: date, equity: float) -> None:
        if self.day != current_date:
            self.day = current_date
            self.day_start_equity = equity
            self.halted = False
            if not self.carry_losses:
                self.consecutive_losses = 0

    def can_trade(self, current_date: date, equity: float) -> bool:
        self._roll_day(current_date, equity)
        if self.halted:
            return False

        dd_pct = ((self.day_start_equity - equity) / max(self.day_start_equity, 1e-9)) * 100
        if dd_pct >= self.settings.max_daily_loss_pct:
            self.halted = True
            return False

        if self.consecutive_losses >= self.settings.max_consecutive_losses:
            self.halted = True
            return False

        return True

    def on_trade_close(self, current_date: date, pnl: float, equity: float) -> None:
        self._roll_day(current_date, equity)
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def calc_position_size(self, equity: float, entry_price: float, stop_price: float) -> float:
        risk_amount = equity * (self.settings.risk_per_trade_pct / 100)
        per_unit_risk = abs(entry_price - stop_price)
        if per_unit_risk <= 0:
            return 0.0
        raw_size = risk_amount / per_unit_risk
        max_size = (equity * self.settings.max_leverage) / max(entry_price, 1e-9)
        return max(0.0, min(raw_size, max_size))


# ---------------------------------------------------------------------------
# Price helpers (match PaperBroker logic)
# ---------------------------------------------------------------------------

def _exec_price(price: float, side: str, slippage_bps: float) -> float:
    slip = slippage_bps / 10_000
    return price * (1 + slip) if side == "buy" else price * (1 - slip)


def _fee(notional: float, fee_bps: float) -> float:
    return notional * (fee_bps / 10_000)


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

def run_backtest(
    settings: Settings,
    candles_by_symbol: Dict[str, List[list]],
    *,
    cooldown_candles: int = 0,
    carry_losses: bool = False,
    htf_candles_by_symbol: Optional[Dict[str, List[list]]] = None,
) -> BTResult:
    min_len = min(len(v) for v in candles_by_symbol.values())
    for sym in list(candles_by_symbol):
        candles_by_symbol[sym] = candles_by_symbol[sym][-min_len:]

    equity = settings.initial_equity
    positions: Dict[str, BTPosition] = {}
    trades: List[BTTrade] = []
    risk = BacktestRisk(settings, carry_losses=carry_losses)
    lookback = settings.lookback_candles
    last_exit_candle: Dict[str, int] = {}
    last_exit_signal: Dict[str, str] = {}

    ref_symbol = settings.symbols[0]
    equity_curve: List[Tuple[datetime, float]] = [
        (datetime.fromtimestamp(candles_by_symbol[ref_symbol][lookback][0] / 1000, tz=timezone.utc), equity)
    ]

    for i in range(lookback, min_len):
        candle_ts = datetime.fromtimestamp(
            candles_by_symbol[ref_symbol][i][0] / 1000, tz=timezone.utc
        )
        candle_date = candle_ts.date()

        for symbol in settings.symbols:
            candle = candles_by_symbol[symbol][i]
            high = float(candle[2])
            low = float(candle[3])
            close = float(candle[4])

            # --- 1. Check exits for open positions (skip entry candle) ---
            if symbol in positions:
                pos = positions[symbol]
                if pos.entry_candle >= i:
                    continue

                hit_stop = hit_tp = False
                if pos.side == "buy":
                    hit_stop = low <= pos.stop_price
                    hit_tp = high >= pos.take_profit_price
                else:
                    hit_stop = high >= pos.stop_price
                    hit_tp = low <= pos.take_profit_price

                if hit_stop or hit_tp:
                    if hit_stop:
                        exit_at = pos.stop_price
                        reason = "stop"
                    else:
                        exit_at = pos.take_profit_price
                        reason = "tp"

                    exit_side = "sell" if pos.side == "buy" else "buy"
                    exit_px = _exec_price(exit_at, exit_side, settings.slippage_bps)
                    notional = exit_px * pos.size
                    exit_fee = _fee(notional, settings.exit_fee_bps)

                    raw_pnl = (exit_px - pos.entry_price) * pos.size
                    if pos.side == "sell":
                        raw_pnl = -raw_pnl
                    net_pnl = raw_pnl - exit_fee
                    equity += net_pnl

                    trades.append(BTTrade(
                        symbol=symbol, side=pos.side, size=pos.size,
                        entry_price=pos.entry_price, exit_price=exit_px,
                        pnl=net_pnl, fees=exit_fee,
                        opened_at=pos.opened_at, closed_at=candle_ts,
                        exit_reason=reason,
                    ))
                    del positions[symbol]
                    risk.on_trade_close(candle_date, net_pnl, equity)
                    if reason == "stop":
                        last_exit_candle[symbol] = i
                        last_exit_signal[symbol] = pos.side
                    continue

                continue  # still in position, don't enter

            # --- 2. Check for new entries ---
            window = candles_by_symbol[symbol][i - lookback: i + 1]
            signal, atr = generate_signal(window, settings)

            if signal == "flat" or atr <= 0:
                continue

            if settings.volume_min_mult > 0:
                if not volume_ok(window, settings.atr_period, settings.volume_min_mult):
                    continue

            if htf_candles_by_symbol and symbol in htf_candles_by_symbol:
                candle_time = candles_by_symbol[symbol][i][0]
                htf_window = [c for c in htf_candles_by_symbol[symbol] if c[0] <= candle_time]
                if len(htf_window) >= lookback:
                    htf_window = htf_window[-lookback:]
                htf = htf_trend(htf_window, settings)
                if htf != signal:
                    continue

            if not risk.can_trade(candle_date, equity):
                continue

            if cooldown_candles > 0 and symbol in last_exit_candle:
                candles_since_exit = i - last_exit_candle[symbol]
                same_direction = (
                    (signal == "long" and last_exit_signal.get(symbol) == "buy")
                    or (signal == "short" and last_exit_signal.get(symbol) == "sell")
                )
                if same_direction and candles_since_exit < cooldown_candles:
                    continue

            stop_dist = atr * settings.stop_atr_multiplier
            if signal == "long":
                stop = close - stop_dist
                take = close + stop_dist * settings.take_profit_r
                side = "buy"
            else:
                stop = close + stop_dist
                take = close - stop_dist * settings.take_profit_r
                side = "sell"

            entry_px = close
            size = risk.calc_position_size(equity, entry_px, stop)
            if size <= 0:
                continue

            entry_fee = _fee(entry_px * size, settings.entry_fee_bps)
            equity -= entry_fee

            positions[symbol] = BTPosition(
                symbol=symbol, side=side, size=size,
                entry_price=entry_px, stop_price=stop,
                take_profit_price=take, opened_at=candle_ts,
                entry_candle=i,
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
    stop_exits = sum(1 for t in trades if t.exit_reason == "stop")
    tp_exits = sum(1 for t in trades if t.exit_reason == "tp")

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
    print(f"  Stop exits:          {stop_exits:>6}")
    print(f"  TP exits:            {tp_exits:>6}")
    print(f"  Max consec losses:   {max_consec:>6}")
    print(f"  Avg trade P&L:       ${net_pnl / n:>+10,.2f}")
    print(f"{'=' * w}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_SETTINGS_FLOATS = {
    "INITIAL_EQUITY", "TARGET_LEVERAGE", "MAX_LEVERAGE", "RISK_PER_TRADE_PCT",
    "MAX_DAILY_LOSS_PCT", "ATR_MIN_PCT", "STOP_ATR_MULTIPLIER", "TAKE_PROFIT_R",
    "ENTRY_FEE_BPS", "EXIT_FEE_BPS", "SLIPPAGE_BPS", "VOLUME_MIN_MULT",
}
_SETTINGS_INTS = {
    "POLL_SECONDS", "LOOKBACK_CANDLES", "MAX_CONSECUTIVE_LOSSES",
    "FAST_EMA", "SLOW_EMA", "ATR_PERIOD", "HEARTBEAT_INTERVAL",
    "COOLDOWN_CANDLES", "LIMIT_TIMEOUT_SECONDS",
}


def _apply_overrides(settings: Settings, overrides: list[str]) -> Settings:
    """Return a new Settings with --set KEY=VALUE overrides applied."""
    kw: dict = {}
    for item in overrides:
        if "=" not in item:
            print(f"WARNING: ignoring malformed override '{item}' (expected KEY=VALUE)")
            continue
        key, val = item.split("=", 1)
        field = key.lower()
        if key in _SETTINGS_FLOATS:
            kw[field] = float(val)
        elif key in _SETTINGS_INTS:
            kw[field] = int(val)
        else:
            kw[field] = val
    return replace(settings, **kw) if kw else settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the trading strategy on cached candle data")
    parser.add_argument("--env", default=".env.production-paper", help="Env file to load settings from")
    parser.add_argument("--data-dir", default="data", help="Directory with cached candle JSON files")
    parser.add_argument("--label", default="BACKTEST", help="Label for the report header")
    parser.add_argument("--set", nargs="*", default=[], dest="overrides",
                        help="Override settings, e.g. --set TAKE_PROFIT_R=1.0 ATR_MIN_PCT=0.15")
    parser.add_argument("--cooldown", type=int, default=0,
                        help="Candles to wait after stop-out before same-direction re-entry (0=off)")
    parser.add_argument("--carry-losses", action="store_true", default=False,
                        help="Don't reset consecutive loss counter at midnight")
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

    htf_candles_by_symbol: Optional[Dict[str, list]] = None
    if settings.htf_timeframe:
        htf_candles_by_symbol = {}
        for symbol in settings.symbols:
            safe = symbol.replace("/", "-").replace(":", "-")
            path = data_dir / f"{safe}_{settings.htf_timeframe}.json"
            if path.exists():
                with path.open(encoding="utf-8") as f:
                    htf_candles_by_symbol[symbol] = json.load(f)
                print(f"Loaded {len(htf_candles_by_symbol[symbol]):,} HTF candles for {symbol}")
            else:
                print(f"NOTE: No HTF data at {path} — HTF filter disabled for {symbol}")
                htf_candles_by_symbol = None
                break

    flags: list[str] = []
    if args.cooldown:
        flags.append(f"cooldown={args.cooldown}")
    if args.carry_losses:
        flags.append("carry-losses")
    if htf_candles_by_symbol:
        flags.append(f"htf={settings.htf_timeframe}")
    if settings.volume_min_mult > 0:
        flags.append(f"vol>={settings.volume_min_mult}x")
    if flags:
        print(f"Flags: {', '.join(flags)}")

    result = run_backtest(
        settings, candles_by_symbol,
        cooldown_candles=args.cooldown,
        carry_losses=args.carry_losses,
        htf_candles_by_symbol=htf_candles_by_symbol,
    )
    print_report(result, label=args.label)


if __name__ == "__main__":
    main()
