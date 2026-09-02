"""Offline backtest of the EMA/ATR trend strategy on cached OHLCV.

Lifecycle per symbol mirrors ``src/bot.py`` exactly, so a backtest result and
live behaviour diverge only through fills, not through logic:
  1. Enter when the trend signals and every opt-in entry filter passes.
  2. Size off the ATR initial stop (``RISK_PER_TRADE_PCT`` of equity at risk).
  3. Ratchet a chandelier trailing stop from the favorable extreme.
  4. Exit on a stop touch, or when the EMA cross flips against the position.

Usage:
    python -m src.fetch_candles                        # download data first
    python -m src.backtest                             # baseline run
    python -m src.backtest --set ADX_MIN=25            # tweak a knob
    python -m src.backtest --label SIGNAL --set DIRECTION_MODE=signal
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from . import direction as direction_mod
from .config import Settings, load_settings
from .exchange import _timeframe_ms
from .indicators import adx_ok, htf_trend_ok, volume_ok
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
class BTPosition:
    symbol: str
    side: str
    size: float
    entry_price: float
    opened_at: datetime
    entry_candle: int
    peak_price: float = 0.0
    stop_price: float = 0.0
    fee_paid: float = 0.0        # entry fee, carried so the trade record is complete


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
    trades: list[BTTrade]
    equity_curve: list[tuple[datetime, float]]
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
        self.halted_until: datetime | None = None

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


# ---------------------------------------------------------------------------
# Price helpers (match PaperBroker logic)
# ---------------------------------------------------------------------------

def _exec_price(price: float, side: str, slippage_bps: float) -> float:
    slip = slippage_bps / 10_000
    return price * (1 + slip) if side == "buy" else price * (1 - slip)


def _fee(notional: float, fee_bps: float) -> float:
    return notional * (fee_bps / 10_000)


def _close_bt_position(
    positions: dict[str, BTPosition],
    trades: list[BTTrade],
    symbol: str,
    pos: BTPosition,
    exit_at: float,
    reason: str,
    settings: Settings,
    candle_ts,
) -> float:
    """Close ``pos`` at ``exit_at``, record the trade, return realized net P&L.

    Direction-aware: a short (``side == "sell"``) profits when the exit price is
    below the entry, so its raw P&L is negated.
    """
    exit_side = "sell" if pos.side == "buy" else "buy"
    exit_px = _exec_price(exit_at, exit_side, settings.slippage_bps)
    exit_fee = _fee(exit_px * pos.size, settings.exit_fee_bps)
    raw_pnl = (exit_px - pos.entry_price) * pos.size
    if pos.side == "sell":
        raw_pnl = -raw_pnl
    net_pnl = raw_pnl - exit_fee
    trades.append(
        BTTrade(
            symbol=symbol,
            side=pos.side,
            size=pos.size,
            entry_price=pos.entry_price,
            exit_price=exit_px,
            pnl=net_pnl,
            fees=exit_fee + pos.fee_paid,
            opened_at=pos.opened_at,
            closed_at=candle_ts,
            exit_reason=reason,
        )
    )
    del positions[symbol]
    return net_pnl


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

def run_backtest(
    settings: Settings,
    candles_by_symbol: dict[str, list[list]],
    htf_by_symbol: dict[str, list[list]] | None = None,
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
    htf_close_times: dict[str, list[int]] = {
        sym: [int(r[0]) + htf_interval_ms for r in rows]
        for sym, rows in htf_by_symbol.items()
    }

    def _htf_window(symbol: str, cur_open_ms: int) -> list[list]:
        rows = htf_by_symbol.get(symbol)
        if not rows:
            return []
        n = bisect.bisect_right(htf_close_times[symbol], cur_open_ms)
        start = max(0, n - settings.lookback_candles)
        return rows[start:n]

    equity = settings.initial_equity
    positions: dict[str, BTPosition] = {}
    trades: list[BTTrade] = []
    risk = BacktestRisk(settings)
    lookback = settings.lookback_candles
    if lookback >= min_len:
        raise ValueError(
            f"Not enough candles: have {min_len}, need > {lookback} (lookback + 1)."
        )

    ref_symbol = settings.symbols[0]
    equity_curve: list[tuple[datetime, float]] = [
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

            closed_window = candles_by_symbol[symbol][i - lookback : i]
            atr = atr_value(closed_window, settings)
            # Under DIRECTION_MODE=signal the trend picks the side; an open
            # position always keeps the one it was entered with.
            direction = direction_mod.resolve(
                symbol, settings, closed_window, positions.get(symbol)
            )
            is_long = direction != "short"
            side = "buy" if is_long else "sell"

            # --- 1. Manage open position (chandelier trail / regime exit) ---
            if symbol in positions:
                pos = positions[symbol]
                # Skip *exit* checks on the entry candle (matches live bot
                # behaviour and prevents a same-bar entry+exit).
                allow_exit = i > pos.entry_candle

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

            # --- 2. No position: enter on a fresh trend signal ---
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
                peak_price=entry_px,
                stop_price=init_stop,
                fee_paid=entry_fee,
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
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
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
    trail_exits = sum(1 for t in trades if t.exit_reason == "trail")
    regime_exits = sum(1 for t in trades if t.exit_reason == "regime")

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
    print(f"  Regime exits:        {regime_exits:>6}")
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
    "ENTRY_FEE_BPS", "EXIT_FEE_BPS", "SLIPPAGE_BPS",
    "CONSEC_HALT_HOURS", "DAILY_LOSS_HALT_HOURS",
    "RISK_PER_TRADE_PCT", "STOP_ATR_MULTIPLIER", "TRAIL_ATR_MULTIPLIER",
    "ADX_MIN", "VOLUME_MIN_MULT",
}
_SETTINGS_INTS = {
    "POLL_SECONDS", "LOOKBACK_CANDLES", "MAX_CONSECUTIVE_LOSSES",
    "HEARTBEAT_INTERVAL", "TREND_EMA_PERIOD",
    "FAST_EMA", "SLOW_EMA", "ATR_PERIOD",
    "ADX_PERIOD", "VOLUME_MA_PERIOD", "MTF_EMA_PERIOD",
}
_SETTINGS_BOOLS = {"MTF_ENABLED"}
# Comma-separated list fields. Without this the raw string lands on the field
# and iterating it yields one character per symbol ("B", "T", "C", ...).
_SETTINGS_LISTS = {"SYMBOLS", "SHORT_SYMBOLS"}
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
        elif key in _SETTINGS_LISTS:
            items = [s.strip() for s in val.split(",") if s.strip()]
            kw[field_name] = [s.upper() for s in items] if key == "SHORT_SYMBOLS" else items
        else:
            kw[field_name] = val
    return replace(settings, **kw) if kw else settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the trend-following strategy on cached candle data")
    parser.add_argument("--env", default=".env.example", help="Env file to load settings from")
    parser.add_argument("--data-dir", default="data", help="Directory with cached candle JSON files")
    parser.add_argument("--label", default="BACKTEST", help="Label for the report header")
    parser.add_argument("--set", nargs="*", default=[], dest="overrides",
                        help="Override settings, e.g. --set DIRECTION_MODE=signal ADX_MIN=25")
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    settings = load_settings()
    settings = _apply_overrides(settings, args.overrides)

    if args.overrides:
        print(f"Overrides applied: {', '.join(args.overrides)}")

    data_dir = Path(args.data_dir)
    candles_by_symbol: dict[str, list] = {}
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
    htf_by_symbol: dict[str, list] = {}
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
        f"direction={settings.direction_mode}",
        f"short_symbols={settings.short_symbols or '-'}",
        f"ema={settings.fast_ema}/{settings.slow_ema}/{settings.trend_ema_period}",
        f"atr={settings.atr_period}",
        f"stop={settings.stop_atr_multiplier}xATR",
        f"trail={settings.trail_atr_multiplier}xATR",
        f"risk={settings.risk_per_trade_pct}%",
        f"adx_min={settings.adx_min}",
        f"vol_mult={settings.volume_min_mult}",
        f"mtf={settings.mtf_enabled}",
        f"consec_halt={settings.consec_halt_hours}h",
        f"daily_halt={settings.daily_loss_halt_hours}h",
    ]
    print(f"Flags: {', '.join(flags)}")

    result = run_backtest(settings, candles_by_symbol, htf_by_symbol)
    print_report(result, label=args.label)


if __name__ == "__main__":
    main()
