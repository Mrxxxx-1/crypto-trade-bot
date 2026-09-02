"""Preflight for DIRECTION_MODE: show the side each symbol would trade right now.

Reads cached candles (run ``python -m src.fetch_candles`` first) and prints the
direction the live bot would resolve on the latest closed bar, plus every entry
gate that stands between that signal and an actual order.

    python scripts/check-direction.py
    python scripts/check-direction.py --mode static   # compare without editing .env
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import direction as direction_mod  # noqa: E402
from src.config import load_settings  # noqa: E402
from src.indicators import adx_ok, compute_adx, htf_trend_ok, volume_ok  # noqa: E402
from src.strategy_trend import (  # noqa: E402
    atr_value,
    initial_stop,
    position_size,
    trend_signal,
)

LONG, SHORT = "long", "short"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("static", "signal"),
                        help="Override DIRECTION_MODE for this check only")
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    settings = load_settings()
    if args.mode:
        settings = replace(settings, direction_mode=args.mode)

    print(f"DIRECTION_MODE = {settings.direction_mode}"
          f"{'  (overridden for this check)' if args.mode else ''}")
    print(f"STRATEGY       = {settings.strategy}   TIMEFRAME = {settings.timeframe}")
    print(f"SHORT_SYMBOLS  = {settings.short_symbols or '(none)'}"
          f"{'   <- ignored in signal mode' if settings.direction_mode == 'signal' else ''}")
    print(f"MAX_LEVERAGE   = {settings.max_leverage}x   "
          f"RISK_PER_TRADE = {settings.risk_per_trade_pct}%")
    print()

    for symbol in settings.symbols:
        safe = symbol.replace("/", "-").replace(":", "-")
        path = Path(args.data_dir) / f"{safe}_{settings.timeframe}.json"
        if not path.exists():
            print(f"{symbol}: no cached data at {path} "
                  f"(run: python -m src.fetch_candles)")
            continue

        rows = json.loads(path.read_text())
        window = rows[-(settings.lookback_candles + 1):-1]
        last = float(window[-1][4])

        static_dir = settings.direction_for(symbol)
        resolved = direction_mod.resolve(symbol, settings, window)
        atr = atr_value(window, settings)
        adx = compute_adx(window, settings.adx_period)
        stop = initial_stop(last, atr, settings, resolved)
        size = position_size(settings.initial_equity, last, stop, settings)

        gates = [
            ("trend signal", trend_signal(window, settings, resolved)),
            (f"ADX >= {settings.adx_min} (is {adx:.1f})", adx_ok(window, settings)),
            ("volume", volume_ok(window, settings)),
            ("HTF align", htf_trend_ok([], settings, resolved) if settings.mtf_enabled else True),
            ("ATR > 0", atr > 0),
            ("size > 0", size > 0),
        ]
        blocked = [name for name, ok in gates if not ok]

        print(f"{symbol}   last={last:,.2f}")
        print(f"   long signal : {trend_signal(window, settings, LONG)}")
        print(f"   short signal: {trend_signal(window, settings, SHORT)}")
        print(f"   -> direction: {resolved}"
              + (f"   (static mode would say {static_dir})" if resolved != static_dir else ""))
        print(f"   atr={atr:,.2f}  stop={stop:,.2f}  size={size:.4f}  "
              f"notional=${size * last:,.2f}")
        if blocked:
            print(f"   NO ENTRY - blocked by: {', '.join(blocked)}")
        else:
            print("   WOULD ENTER on the next poll")
        print()


if __name__ == "__main__":
    main()
