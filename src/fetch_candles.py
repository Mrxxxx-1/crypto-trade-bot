"""Download and cache historical OHLCV candle data from Hyperliquid.

Usage:
    python -m src.fetch_candles
    python -m src.fetch_candles --symbols BTC/USDC:USDC --days 60 --timeframe 5m
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt


def fetch_symbol(
    exchange: ccxt.hyperliquid,
    symbol: str,
    timeframe: str,
    since_ms: int,
    limit_per_req: int = 100,
) -> list[list]:
    """Paginate OHLCV from Hyperliquid, deduplicate, and return sorted candles."""
    all_candles: list[list] = []
    cursor = since_ms

    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=limit_per_req)
        if not batch:
            break
        all_candles.extend(batch)
        cursor = batch[-1][0] + 1
        if len(batch) < limit_per_req:
            break
        time.sleep(exchange.rateLimit / 1000)

    seen: set[int] = set()
    unique: list[list] = []
    for c in all_candles:
        ts = int(c[0])
        if ts not in seen:
            seen.add(ts)
            unique.append(c)
    unique.sort(key=lambda c: c[0])
    return unique


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch & cache OHLCV data")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USDC:USDC", "ETH/USDC:USDC"])
    parser.add_argument("--timeframes", nargs="+", default=["5m", "1h"])
    parser.add_argument("--days", type=int, default=45)
    parser.add_argument("--out", default="data")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    exchange = ccxt.hyperliquid({"enableRateLimit": True})
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp() * 1000)

    for symbol in args.symbols:
        for tf in args.timeframes:
            print(f"Fetching {symbol} {tf} ({args.days} days)...")
            candles = fetch_symbol(exchange, symbol, tf, since_ms)
            if not candles:
                print(f"  WARNING: no data returned for {symbol} {tf}")
                continue

            safe_name = symbol.replace("/", "-").replace(":", "-")
            path = out_dir / f"{safe_name}_{tf}.json"
            with path.open("w", encoding="utf-8") as f:
                json.dump(candles, f)

            first_dt = datetime.fromtimestamp(candles[0][0] / 1000, tz=timezone.utc)
            last_dt = datetime.fromtimestamp(candles[-1][0] / 1000, tz=timezone.utc)
            print(f"  Saved {len(candles)} candles  {first_dt:%Y-%m-%d} -> {last_dt:%Y-%m-%d}  [{path}]")

    print("Done.")


if __name__ == "__main__":
    main()
