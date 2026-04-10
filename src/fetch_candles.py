"""Download and cache historical OHLCV candle data from Hyperliquid (official SDK).

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

from hyperliquid.info import Info
from hyperliquid.utils.constants import MAINNET_API_URL

from .exchange import _candles_to_rows, hl_coin


def fetch_symbol(
    info: Info,
    symbol: str,
    timeframe: str,
    since_ms: int,
    chunk_ms: int = 7 * 24 * 3600 * 1000,
) -> list[list]:
    """Paginate ``candleSnapshot`` windows, deduplicate by open time, return sorted rows."""
    coin = hl_coin(symbol)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cursor = since_ms
    all_rows: dict[int, list] = {}

    while cursor < end_ms:
        batch_end = min(cursor + chunk_ms, end_ms)
        batch = info.candles_snapshot(coin, timeframe, cursor, batch_end)
        for row in _candles_to_rows(batch):
            ts = int(row[0])
            if ts not in all_rows:
                all_rows[ts] = row
        cursor = batch_end
        time.sleep(0.2)

    unique = sorted(all_rows.values(), key=lambda r: r[0])
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

    info = Info(MAINNET_API_URL, skip_ws=True, timeout=30.0)
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp() * 1000)

    for symbol in args.symbols:
        for tf in args.timeframes:
            print(f"Fetching {symbol} {tf} ({args.days} days)...")
            candles = fetch_symbol(info, symbol, tf, since_ms)
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
