"""Backtest the dual-leg straddle: open long + short, cut the loser, run the winner.

Simulation only. Hyperliquid holds one net position per coin, so two opposing
legs on the same symbol would cancel out in a live account; settling whether the
idea has an edge is cheaper here than building a second sub-account first.

Per symbol the engine tracks two independent legs under the keys ``BTC|long``
and ``BTC|short``, both sized off the same equity and the same ATR stop distance
so they open at matching size. A configurable trigger declares the winner; the
loser is closed (``exit_reason="cut_loser"``) and the winner keeps the ordinary
chandelier trail and regime exit from ``src/strategy_trend.py``.

Usage:
    python -m src.fetch_candles --timeframes 4h
    python -m src.backtest_straddle --env .env --compare
    python -m src.backtest_straddle --env .env --trigger atr --trigger-atr-mult 1.5
"""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

from .backtest import (
    BacktestRisk,
    BTLeg,
    BTPosition,
    BTResult,
    BTTrade,
    _apply_overrides,
    _close_bt_position,
    _exec_price,
    _fee,
    print_report,
    run_backtest,
)
from .config import Settings, load_settings
from .strategy_squeeze import SqueezeConfig
from .strategy_straddle import (
    LONG,
    SHORT,
    StraddleConfig,
    opposite,
    should_open_pair,
    trigger_level,
    winner_direction,
)
from .strategy_trend import (
    atr_value,
    chandelier_stop,
    initial_stop,
    position_size,
    regime_intact,
)

DIRECTIONS = (LONG, SHORT)

# How a pair ended up with (or without) a winner.
CUT_BY_TRIGGER = "trigger"
CUT_BY_STOP = "stop_pre_trigger"
CUT_BOTH_STOPPED = "both_stopped"


def leg_key(symbol: str, direction: str) -> str:
    return f"{symbol}|{direction}"


@dataclass
class PairRecord:
    """One straddle cycle: both legs opened together, both now closed."""

    symbol: str
    opened_at: datetime
    closed_at: Optional[datetime]
    ref_price: float
    size: float
    winner: Optional[str]
    cut_reason: str
    cut_at: Optional[datetime]
    cut_price: float
    leg_pnl: Dict[str, float] = field(default_factory=dict)
    leg_fees: Dict[str, float] = field(default_factory=dict)

    @property
    def net_pnl(self) -> float:
        return sum(self.leg_pnl.values())

    @property
    def fees(self) -> float:
        return sum(self.leg_fees.values())

    @property
    def loser(self) -> Optional[str]:
        return opposite(self.winner) if self.winner else None

    @property
    def winner_pnl(self) -> Optional[float]:
        return self.leg_pnl.get(self.winner) if self.winner else None

    @property
    def loser_pnl(self) -> Optional[float]:
        loser = self.loser
        return self.leg_pnl.get(loser) if loser else None

    @property
    def loser_fees(self) -> float:
        """Fees a single directional entry would not have paid."""
        loser = self.loser
        if loser is None:
            return self.fees
        return self.leg_fees.get(loser, 0.0)


@dataclass
class _OpenPair:
    symbol: str
    opened_at: datetime
    entry_candle: int
    ref_price: float
    size: float
    open_legs: set
    winner: Optional[str] = None
    cut_reason: str = ""
    cut_at: Optional[datetime] = None
    cut_price: float = 0.0
    regime_armed: bool = False
    leg_pnl: Dict[str, float] = field(default_factory=dict)
    leg_fees: Dict[str, float] = field(default_factory=dict)


@dataclass
class StraddleResult(BTResult):
    pairs: List[PairRecord] = field(default_factory=list)


def _finalize(pair: _OpenPair, closed_at: datetime) -> PairRecord:
    return PairRecord(
        symbol=pair.symbol,
        opened_at=pair.opened_at,
        closed_at=closed_at,
        ref_price=pair.ref_price,
        size=pair.size,
        winner=pair.winner,
        cut_reason=pair.cut_reason or CUT_BOTH_STOPPED,
        cut_at=pair.cut_at,
        cut_price=pair.cut_price,
        leg_pnl=dict(pair.leg_pnl),
        leg_fees=dict(pair.leg_fees),
    )


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

def run_straddle_backtest(
    settings: Settings,
    candles_by_symbol: Dict[str, List[list]],
    cfg: StraddleConfig,
) -> StraddleResult:
    """Replay candles opening a mirrored pair per symbol and cutting the loser."""
    cfg.validate()

    min_len = min(len(v) for v in candles_by_symbol.values())
    for sym in list(candles_by_symbol):
        candles_by_symbol[sym] = candles_by_symbol[sym][-min_len:]

    equity = settings.initial_equity
    positions: Dict[str, BTPosition] = {}
    trades: List[BTTrade] = []
    pairs: Dict[str, _OpenPair] = {}
    records: List[PairRecord] = []
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

    def close_leg(
        pair: _OpenPair,
        direction: str,
        pos: BTPosition,
        exit_at: float,
        reason: str,
        candle_ts: datetime,
    ) -> float:
        net_pnl = _close_bt_position(
            positions,
            trades,
            pair.symbol,
            pos,
            exit_at,
            reason,
            settings,
            candle_ts,
            key=leg_key(pair.symbol, direction),
        )
        pair.open_legs.discard(direction)
        pair.leg_pnl[direction] = net_pnl
        pair.leg_fees[direction] = trades[-1].fees
        return net_pnl

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

            pair = pairs.get(symbol)
            if pair is not None:
                # Never enter and exit on the same bar (mirrors run_backtest).
                allow_exit = i > pair.entry_candle

                # --- 1. Trail and stop each open leg independently ---
                for direction in DIRECTIONS:
                    pos = positions.get(leg_key(symbol, direction))
                    if pos is None:
                        continue
                    is_long = direction == LONG
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
                        # A leg stopped before the trigger fires forfeits by
                        # default and hands the pair to the survivor.
                        pre_trigger = pair.winner is None
                        reason = "stop_pre_trigger" if pre_trigger else "trail"
                        net_pnl = close_leg(
                            pair, direction, pos, pos.stop_price, reason, candle_ts
                        )
                        equity += net_pnl
                        risk.on_trade_close(candle_ts, net_pnl, equity)
                        if pre_trigger and pair.open_legs:
                            pair.winner = opposite(direction)
                            pair.cut_reason = CUT_BY_STOP
                            pair.cut_at = candle_ts
                            pair.cut_price = pos.stop_price

                # --- 2. Trigger: declare a winner and cut the loser ---
                if allow_exit and pair.winner is None and len(pair.open_legs) == 2:
                    winner = winner_direction(
                        closed_window, settings, cfg, pair.ref_price, high, low, atr
                    )
                    if winner is not None:
                        level = trigger_level(
                            closed_window, cfg, pair.ref_price, winner, atr
                        )
                        cut_at = close if level <= 0 else min(max(level, low), high)
                        loser = opposite(winner)
                        loser_pos = positions[leg_key(symbol, loser)]
                        net_pnl = close_leg(
                            pair, loser, loser_pos, cut_at, "cut_loser", candle_ts
                        )
                        equity += net_pnl
                        risk.on_trade_close(candle_ts, net_pnl, equity)
                        pair.winner = winner
                        pair.cut_reason = CUT_BY_TRIGGER
                        pair.cut_at = candle_ts
                        pair.cut_price = cut_at

                # --- 3. The winner exits when its trend dies ---
                # The regime exit only arms once the EMA cross has actually
                # confirmed the winner. The ATR and range triggers fire before
                # the slow cross flips, so without this the winner would be
                # closed on the very bar it was handed the pair.
                if allow_exit and pair.winner is not None:
                    pos = positions.get(leg_key(symbol, pair.winner))
                    if pos is not None:
                        if regime_intact(closed_window, settings, pair.winner):
                            pair.regime_armed = True
                        elif pair.regime_armed:
                            net_pnl = close_leg(
                                pair, pair.winner, pos, close, "regime", candle_ts
                            )
                            equity += net_pnl
                            risk.on_trade_close(candle_ts, net_pnl, equity)

                if not pair.open_legs:
                    records.append(_finalize(pair, candle_ts))
                    del pairs[symbol]

                # A live or just-retired pair blocks re-entry on this bar.
                continue

            # --- 4. Flat: maybe open a fresh pair ---
            if atr <= 0:
                continue
            if not should_open_pair(closed_window, settings, cfg.entry_mode, cfg.squeeze):
                continue
            if not risk.can_trade(candle_ts, equity):
                continue

            long_stop = initial_stop(close, atr, settings, LONG)
            short_stop = initial_stop(close, atr, settings, SHORT)
            # One size for both legs: the ATR stop is symmetric, so this is the
            # size position_size() would return for either side.
            size = position_size(equity, close, long_stop, settings)
            if size <= 0:
                continue

            new_pair = _OpenPair(
                symbol=symbol,
                opened_at=candle_ts,
                entry_candle=i,
                ref_price=close,
                size=size,
                open_legs=set(DIRECTIONS),
            )
            for direction, stop in ((LONG, long_stop), (SHORT, short_stop)):
                side = "buy" if direction == LONG else "sell"
                entry_px = _exec_price(close, side, settings.slippage_bps)
                entry_fee = _fee(entry_px * size, settings.entry_fee_bps)
                equity -= entry_fee
                positions[leg_key(symbol, direction)] = BTPosition(
                    symbol=symbol,
                    side=side,
                    size=size,
                    entry_price=entry_px,
                    opened_at=candle_ts,
                    entry_candle=i,
                    last_fill_price=entry_px,
                    peak_price=entry_px,
                    stop_price=stop,
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
            pairs[symbol] = new_pair

        equity_curve.append((candle_ts, equity))

    return StraddleResult(
        trades=trades,
        equity_curve=equity_curve,
        starting_equity=settings.initial_equity,
        final_equity=equity,
        pairs=records,
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_straddle_report(result: StraddleResult, label: str = "STRADDLE") -> None:
    """The standard trade report plus pair-level economics."""
    print_report(result, label=label)

    pairs = result.pairs
    w = 52
    print(f"{'=' * w}")
    print(f"  {label} - PAIR ECONOMICS")
    print(f"{'=' * w}")
    if not pairs:
        print("  No pairs opened.")
        print(f"{'=' * w}\n")
        return

    by_trigger = [p for p in pairs if p.cut_reason == CUT_BY_TRIGGER]
    by_stop = [p for p in pairs if p.cut_reason == CUT_BY_STOP]
    both_stopped = [p for p in pairs if p.cut_reason == CUT_BOTH_STOPPED]

    winner_pnls = [p.winner_pnl for p in pairs if p.winner_pnl is not None]
    loser_pnls = [p.loser_pnl for p in pairs if p.loser_pnl is not None]
    net_pnls = [p.net_pnl for p in pairs]
    profitable = [p for p in pairs if p.net_pnl > 0]
    extra_fees = sum(p.loser_fees for p in pairs)
    long_wins = sum(1 for p in pairs if p.winner == LONG)
    short_wins = sum(1 for p in pairs if p.winner == SHORT)

    def _avg(values: List[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    n = len(pairs)
    print(f"  Pairs opened:        {n:>6}")
    print(f"  Cut by trigger:      {len(by_trigger):>6}  ({len(by_trigger)/n*100:.0f}%)")
    print(f"  Cut by early stop:   {len(by_stop):>6}  ({len(by_stop)/n*100:.0f}%)")
    print(f"  Both legs stopped:   {len(both_stopped):>6}  ({len(both_stopped)/n*100:.0f}%)")
    print(f"  Winner long/short:   {long_wins:>6} / {short_wins}")
    print(f"{'-' * w}")
    print(f"  Profitable pairs:    {len(profitable)}/{n}  ({len(profitable)/n*100:.1f}%)")
    print(f"  Avg winner leg:      ${_avg(winner_pnls):>+10,.2f}")
    print(f"  Avg loser leg:       ${_avg(loser_pnls):>+10,.2f}")
    print(f"  Avg net per pair:    ${_avg(net_pnls):>+10,.2f}")
    print(f"  Best pair:           ${max(net_pnls):>+10,.2f}")
    print(f"  Worst pair:          ${min(net_pnls):>+10,.2f}")
    print(f"{'-' * w}")
    print(f"  Loser-leg fee drag:  ${extra_fees:>10,.2f}   (vs a single entry)")
    print(f"  Net P&L before drag: ${sum(net_pnls) + extra_fees:>+10,.2f}")
    print(f"{'=' * w}\n")


def print_comparison(baseline: BTResult, straddle: StraddleResult, cfg: StraddleConfig) -> None:
    """Head-to-head deltas between the baseline trend run and the straddle."""

    def stats(result: BTResult) -> Dict[str, float]:
        trades = result.trades
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        peak = result.starting_equity
        max_dd = 0.0
        for _, eq in result.equity_curve:
            peak = max(peak, eq)
            max_dd = max(max_dd, peak - eq)
        return {
            "net": result.final_equity - result.starting_equity,
            "pf": gross_win / gross_loss if gross_loss > 0 else float("inf"),
            "trades": float(len(trades)),
            "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
            "max_dd": max_dd,
            "fees": sum(t.fees for t in trades),
        }

    b = stats(baseline)
    s = stats(straddle)
    w = 62
    print(f"{'=' * w}")
    print("  HEAD-TO-HEAD  -  baseline trend  vs  straddle")
    print(f"  Straddle config: {cfg.describe()}")
    print(f"{'=' * w}")
    print(f"  {'Metric':<20}{'Baseline':>13}{'Straddle':>13}{'Delta':>14}")
    print(f"{'-' * w}")

    def row(name: str, key: str, fmt: str = "money", signed: bool = True) -> None:
        bv, sv = b[key], s[key]

        def cell(v: float, force_sign: bool) -> str:
            sign = "+" if force_sign else ""
            if fmt == "money":
                return f"${v:{sign},.2f}"
            if fmt == "pct":
                return f"{v:{sign}.1f}%"
            return f"{v:{sign},.2f}"

        print(
            f"  {name:<20}"
            f"{cell(bv, signed):>13}{cell(sv, signed):>13}{cell(sv - bv, True):>14}"
        )

    row("Net P&L", "net")
    row("Max drawdown", "max_dd", signed=False)
    row("Total fees", "fees", signed=False)
    row("Profit factor", "pf", "num", signed=False)
    row("Trades", "trades", "num", signed=False)
    row("Win rate", "win_rate", "pct", signed=False)
    print(f"{'-' * w}")
    verdict = (
        "straddle wins" if s["net"] > b["net"]
        else "baseline wins" if s["net"] < b["net"]
        else "tie"
    )
    print(f"  Verdict on net P&L:  {verdict}")
    print(f"{'=' * w}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_candles(settings: Settings, data_dir: Path) -> Dict[str, list]:
    candles_by_symbol: Dict[str, list] = {}
    for symbol in settings.symbols:
        safe = symbol.replace("/", "-").replace(":", "-")
        path = data_dir / f"{safe}_{settings.timeframe}.json"
        if not path.exists():
            print(f"ERROR: No cached data at {path}")
            print(f"Run first:  python -m src.fetch_candles --timeframes {settings.timeframe}")
            sys.exit(1)
        with path.open(encoding="utf-8") as f:
            candles_by_symbol[symbol] = json.load(f)
        print(f"Loaded {len(candles_by_symbol[symbol]):,} candles for {symbol}")
    return candles_by_symbol


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest the dual-leg straddle (long + short, cut the loser)"
    )
    parser.add_argument("--env", default=".env.example", help="Env file to load settings from")
    parser.add_argument("--data-dir", default="data", help="Directory with cached candle JSON files")
    parser.add_argument("--label", default="STRADDLE", help="Label for the report header")
    parser.add_argument("--entry-mode", default="always", choices=["always", "chop", "squeeze"],
                        help="always = re-open whenever flat; chop = only while ADX < ADX_MIN; "
                             "squeeze = only while volatility is compressed")
    parser.add_argument("--squeeze-lookback", type=int, default=120,
                        help="Bars of history the squeeze percentiles rank against")
    parser.add_argument("--squeeze-atr-pct", type=float, default=20.0,
                        help="Max ATR percentile to count as compressed (0 disables the check)")
    parser.add_argument("--squeeze-bbw-pct", type=float, default=20.0,
                        help="Max Bollinger-width percentile to count as compressed (0 disables)")
    parser.add_argument("--squeeze-nr", type=int, default=0,
                        help="Also require the narrowest bar range of the last N bars (0 disables)")
    parser.add_argument("--squeeze-combine", default="all", choices=["all", "any"],
                        help="Require every enabled squeeze check, or just one")
    parser.add_argument("--trigger", default="trend_signal",
                        choices=["trend_signal", "atr", "range"],
                        help="Which rule declares the winning leg")
    parser.add_argument("--trigger-atr-mult", type=float, default=1.0,
                        help="ATR trigger: cut the leg that is k ATRs offside")
    parser.add_argument("--range-lookback", type=int, default=20,
                        help="Range trigger: bars whose high/low must break")
    parser.add_argument("--compare", action="store_true",
                        help="Also run the baseline trend strategy over identical candles")
    parser.add_argument("--set", nargs="*", default=[], dest="overrides",
                        help="Override settings, e.g. --set RISK_PER_TRADE_PCT=0.5")
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    settings = load_settings()
    settings = _apply_overrides(settings, args.overrides)

    if args.overrides:
        print(f"Overrides applied: {', '.join(args.overrides)}")

    cfg = StraddleConfig(
        entry_mode=args.entry_mode,
        trigger=args.trigger,
        trigger_atr_mult=args.trigger_atr_mult,
        range_lookback=args.range_lookback,
        squeeze=SqueezeConfig(
            lookback=args.squeeze_lookback,
            atr_period=settings.atr_period,
            atr_pct_max=args.squeeze_atr_pct,
            bbw_pct_max=args.squeeze_bbw_pct,
            nr_lookback=args.squeeze_nr,
            combine=args.squeeze_combine,
        ),
    )
    cfg.validate()

    candles_by_symbol = _load_candles(settings, Path(args.data_dir))
    print(
        f"Flags: {cfg.describe()}, timeframe={settings.timeframe}, "
        f"risk={settings.risk_per_trade_pct}%, stop={settings.stop_atr_multiplier}xATR, "
        f"trail={settings.trail_atr_multiplier}xATR"
    )

    if args.compare:
        if settings.strategy != "trend":
            print(
                f"WARNING: STRATEGY={settings.strategy}, so the baseline runs the DCA path. "
                "Use --env .env (or --set STRATEGY=trend) for a like-for-like comparison."
            )
        baseline = run_backtest(settings, deepcopy(candles_by_symbol))
        print_report(baseline, label="BASELINE (trend)")

    result = run_straddle_backtest(settings, deepcopy(candles_by_symbol), cfg)
    print_straddle_report(result, label=args.label)

    if args.compare:
        print_comparison(baseline, result, cfg)


if __name__ == "__main__":
    main()
