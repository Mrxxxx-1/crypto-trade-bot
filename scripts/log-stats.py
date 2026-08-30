#!/usr/bin/env python3
"""Report how big logs/events.jsonl is and how much of it is heartbeat detail.

Use this to confirm log slimming (src/log_hygiene.py) is in effect.

    python scripts/log-stats.py                  # whole file
    python scripts/log-stats.py --since 2026-08-30T19:28   # only a recent run
    python scripts/log-stats.py --logs demo_logs
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", default="logs", help="logs directory (default: logs)")
    parser.add_argument("--since", default="", help="only count lines with ts >= this ISO prefix")
    parser.add_argument("--tail", type=int, default=8, help="how many recent lines to show")
    args = parser.parse_args()

    logs = Path(args.logs)
    events = logs / "events.jsonl"
    if not events.is_file():
        print(f"No {events} yet.")
        return 1

    kinds: Counter[str] = Counter()
    verbose = slim = 0
    verbose_bytes = slim_bytes = 0
    total_bytes = 0
    recent: list[tuple[str, str, bool, int]] = []

    with events.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = str(row.get("ts", ""))
            if args.since and ts < args.since:
                continue

            size = len(line) + 1
            total_bytes += size
            event = str(row.get("event", "?"))
            kinds[event] += 1
            is_verbose = "positions" in row
            if event == "heartbeat":
                if is_verbose:
                    verbose += 1
                    verbose_bytes += size
                else:
                    slim += 1
                    slim_bytes += size
            recent.append((ts, event, is_verbose, size))

    heartbeats = verbose + slim
    print(f"{events}  ({total_bytes / 1024 / 1024:.2f} MB counted)")
    if args.since:
        print(f"filtered to ts >= {args.since}")
    print(f"lines: {sum(kinds.values())}")
    for event, count in kinds.most_common():
        print(f"  {event}: {count}")

    if heartbeats:
        print()
        print(f"heartbeats: {heartbeats}  (verbose {verbose} / slim {slim})")
        if verbose:
            print(f"  avg verbose line: {verbose_bytes / verbose:.0f} bytes")
        if slim:
            print(f"  avg slim line:    {slim_bytes / slim:.0f} bytes")
        if verbose and slim:
            saved = (verbose_bytes / verbose) - (slim_bytes / slim)
            print(f"  saved per slim line: ~{saved:.0f} bytes")
        if slim == 0:
            print("  NOTE: no slim heartbeats — slimming may be off or the run is new.")

    state = logs / "state.json"
    print()
    if state.is_file():
        snap = json.loads(state.read_text(encoding="utf-8"))
        positions = snap.get("positions") or []
        print(f"{state}: {state.stat().st_size} bytes, ts={snap.get('ts')}, "
              f"{len(positions)} open position(s)")
    else:
        print(f"{state}: missing (written on the first heartbeat)")

    if args.tail and recent:
        print()
        print(f"last {min(args.tail, len(recent))} lines:")
        for ts, event, is_verbose, size in recent[-args.tail:]:
            kind = "VERBOSE" if is_verbose else "slim"
            print(f"  {ts[:19]}  {event:<12} {kind:<8} {size} bytes")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
