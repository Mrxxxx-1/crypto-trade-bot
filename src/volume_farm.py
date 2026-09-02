"""Generate exchange volume in round trips, to clear a volume-gated feature.

Hyperliquid gates sub-account creation behind $100,000 of lifetime traded
volume. This tool clears that gate by opening a position at market and closing
it immediately, repeatedly. Each fill counts toward volume, so one round trip of
notional *N* contributes *2N*.

It is a churn tool, not a wash-trading tool: every order crosses the real order
book against real counterparties. It never places both sides of a match itself.

**It is meant for testnet**, where the fees are paid in faucet USDC and the
whole exercise is free. On mainnet the same volume costs real money at the taker
rate for no return, so mainnet requires an explicit override flag.

The important hazard is not fees, it is collision. Hyperliquid holds one net
position per coin, and the trading bot may already hold one. A close here would
close *the bot's* position and desync its state. So this refuses to touch any
coin in ``SYMBOLS`` unless forced, and aborts if the target coin already has an
open position.

Usage:
    python -m src.volume_farm --target 97055                    # dry run: show the plan
    python -m src.volume_farm --target 97055 --execute          # do it (testnet)
    python -m src.volume_farm --target 97055 --coin SOL --execute
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from hyperliquid.info import Info
from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL

from .config import Settings, load_settings
from .exchange import ExchangeAdapter, hl_coin

# Hyperliquid's documented gate for creating a sub-account.
SUBACCOUNT_VOLUME_REQUIREMENT = 100_000.0

# Leave headroom under the leverage cap so a tick against us cannot reject the
# order for insufficient margin.
MARGIN_SAFETY = 0.80


@dataclass
class FarmPlan:
    """What a run intends to do, before any order is placed."""

    symbol: str
    target_volume: float
    price: float
    equity: float
    leverage: float
    notional_per_trip: float
    size_per_trip: float
    trips: int
    taker_rate: float

    @property
    def volume_per_trip(self) -> float:
        return self.notional_per_trip * 2.0

    @property
    def estimated_fees(self) -> float:
        return self.target_volume * self.taker_rate

    def describe(self) -> str:
        return (
            f"{self.symbol}: {self.trips} round trips of "
            f"{self.size_per_trip:.6f} (~${self.notional_per_trip:,.0f} notional) "
            f"at {self.leverage:g}x -> ${self.volume_per_trip:,.0f} volume each"
        )


@dataclass
class FarmResult:
    volume: float = 0.0
    trips: int = 0
    fees: float = 0.0
    realized_pnl: float = 0.0
    errors: list[str] = field(default_factory=list)
    aborted: str = ""

    @property
    def ok(self) -> bool:
        return not self.aborted and not self.errors


BASE_TAKER_RATE = 0.00045


def _user_fees(settings: Settings) -> dict:
    """Fee schedule and daily volume for the configured account."""
    addr = settings.wallet_address.strip()
    if not addr:
        return {}
    base = TESTNET_API_URL if settings.testnet else MAINNET_API_URL
    try:
        return Info(base_url=base, skip_ws=True, timeout=15.0).user_fees(addr) or {}
    except Exception:  # noqa: BLE001
        return {}


def _taker_rate(settings: Settings) -> float:
    """The account's cross (taker) fee rate, falling back to the base tier."""
    return float(_user_fees(settings).get("userCrossRate") or BASE_TAKER_RATE)


def today_volume(settings: Settings) -> Optional[float]:
    """Volume the account has traded today, as the exchange counts it."""
    rows = _user_fees(settings).get("dailyUserVlm") or []
    if not rows:
        return None
    last = rows[-1]
    return float(last.get("userCross", 0) or 0) + float(last.get("userAdd", 0) or 0)


def plan_farm(
    settings: Settings,
    adapter: ExchangeAdapter,
    symbol: str,
    target_volume: float,
    leverage: float,
    max_notional: float = 0.0,
) -> FarmPlan:
    """Size the round trips. Raises ValueError when it cannot be done safely."""
    if target_volume <= 0:
        raise ValueError("target volume must be positive")
    if leverage <= 0:
        raise ValueError("leverage must be positive")

    price = float(adapter.fetch_last_price(symbol))
    if price <= 0:
        raise ValueError(f"no price for {symbol}")

    equity = float(adapter.fetch_balance())
    if equity <= 0:
        raise ValueError("account has no equity to trade with")

    notional = equity * leverage * MARGIN_SAFETY
    if max_notional > 0:
        notional = min(notional, max_notional)

    size = adapter.round_size(symbol, notional / price)
    if size <= 0:
        raise ValueError(
            f"{symbol} size rounds to zero at ${notional:,.2f} notional; "
            "raise --leverage or pick a cheaper coin"
        )

    notional = size * price
    trips = max(1, int(target_volume / (notional * 2.0)) + 1)
    return FarmPlan(
        symbol=symbol,
        target_volume=target_volume,
        price=price,
        equity=equity,
        leverage=leverage,
        notional_per_trip=notional,
        size_per_trip=size,
        trips=trips,
        taker_rate=_taker_rate(settings),
    )


def check_collisions(settings: Settings, adapter: ExchangeAdapter, symbol: str, force: bool) -> list[str]:
    """Reasons this coin is unsafe to churn right now."""
    problems: list[str] = []
    coin = hl_coin(symbol)

    bot_coins = {hl_coin(s) for s in settings.symbols}
    if coin in bot_coins and not force:
        problems.append(
            f"{coin} is in SYMBOLS, so the bot may hold a net position on it. "
            "Churning it would close the bot's position. Pick another coin, or "
            "pass --allow-bot-coin after pausing the bot and confirming it is flat."
        )

    try:
        positions = adapter.fetch_positions()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"could not read positions to check for collisions: {exc}")
        return problems

    for pos in positions:
        if str(pos.get("coin", "")).upper() == coin.upper():
            problems.append(
                f"{coin} already has an open {pos.get('side')} position of "
                f"{pos.get('size')}; close it before farming volume on this coin."
            )
    return problems


class VolumeFarmer:
    """Executes the plan, tracking volume from actual fills."""

    def __init__(
        self,
        settings: Settings,
        adapter: ExchangeAdapter,
        pause: float = 1.0,
        max_equity_drop_pct: float = 10.0,
        sleep: Optional[Callable[[float], None]] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.settings = settings
        self.adapter = adapter
        self.pause = pause
        self.max_equity_drop_pct = max_equity_drop_pct
        self._sleep = sleep or time.sleep
        self._log = log or print

    def _round_trip(self, plan: FarmPlan) -> tuple[float, float]:
        """One open-and-close. Returns (volume, notional_pnl_estimate)."""
        symbol, size = plan.symbol, plan.size_per_trip

        opened = self.adapter.create_market_order(symbol, "buy", size, params={})
        open_px = float(opened.get("average") or plan.price)
        open_sz = float(opened.get("filled") or size)
        volume = open_px * open_sz

        # Close exactly what filled, so a partial fill cannot leave a residue.
        closed = self.adapter.create_market_order(
            symbol, "sell", open_sz, params={"reduceOnly": True}
        )
        close_px = float(closed.get("average") or open_px)
        close_sz = float(closed.get("filled") or open_sz)
        volume += close_px * close_sz

        return volume, (close_px - open_px) * close_sz

    def run(self, plan: FarmPlan) -> FarmResult:
        result = FarmResult()
        start_equity = plan.equity
        floor = start_equity * (1 - self.max_equity_drop_pct / 100.0)

        while result.volume < plan.target_volume:
            if result.trips >= plan.trips * 3:
                result.aborted = "trip cap reached without hitting the target"
                break
            try:
                volume, pnl = self._round_trip(plan)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(str(exc))
                self._log(f"  ! trip {result.trips + 1} failed: {exc}")
                if len(result.errors) >= 3:
                    result.aborted = "three consecutive failures"
                    break
                self._sleep(self.pause)
                continue

            result.errors.clear()
            result.trips += 1
            result.volume += volume
            result.realized_pnl += pnl
            result.fees += volume * plan.taker_rate
            pct = min(100.0, result.volume / plan.target_volume * 100)
            self._log(
                f"  trip {result.trips}: +${volume:,.0f} volume "
                f"(total ${result.volume:,.0f}, {pct:.0f}%) pnl {pnl:+.2f}"
            )

            try:
                equity = float(self.adapter.fetch_balance())
            except Exception:  # noqa: BLE001
                equity = start_equity + result.realized_pnl - result.fees
            if equity < floor:
                result.aborted = (
                    f"equity fell to ${equity:,.2f}, below the "
                    f"{self.max_equity_drop_pct:g}% floor of ${floor:,.2f}"
                )
                break

            if result.volume < plan.target_volume:
                self._sleep(self.pause)

        return result


def _print_plan(plan: FarmPlan, settings: Settings, remaining: Optional[float]) -> None:
    width = 66
    print()
    print("=" * width)
    print("  VOLUME FARM PLAN")
    print("=" * width)
    print(f"  Network:            {'TESTNET' if settings.testnet else 'MAINNET (real money)'}")
    print(f"  Coin:               {plan.symbol}")
    print(f"  Price:              ${plan.price:,.2f}")
    print(f"  Account equity:     ${plan.equity:,.2f}")
    print(f"  Per trip:           {plan.size_per_trip:.6f} (~${plan.notional_per_trip:,.2f}) at {plan.leverage:g}x")
    print(f"  Volume per trip:    ${plan.volume_per_trip:,.2f}")
    print(f"  Target volume:      ${plan.target_volume:,.2f}")
    print(f"  Round trips needed: {plan.trips}")
    print("-" * width)
    print(f"  Taker rate:         {plan.taker_rate * 100:.4f}% per side")
    currency = "testnet USDC (free from the faucet)" if settings.testnet else "REAL USDC"
    print(f"  Estimated fees:     ${plan.estimated_fees:,.2f} in {currency}")
    if remaining is not None:
        print(f"  Gate remaining:     ${remaining:,.2f} to reach ${SUBACCOUNT_VOLUME_REQUIREMENT:,.0f}")
    print("=" * width)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate traded volume in round trips to clear a volume gate"
    )
    parser.add_argument("--target", type=float, required=True,
                        help="Volume to generate, in USD (both sides of a trip count)")
    parser.add_argument("--coin", default="SOL",
                        help="Coin to churn. Must not be one the bot trades. Default SOL.")
    parser.add_argument("--leverage", type=float, default=5.0,
                        help="Leverage per trip; higher means fewer trips, same total fees")
    parser.add_argument("--max-notional", type=float, default=0.0,
                        help="Hard cap on notional per trip (0 = no cap)")
    parser.add_argument("--pause", type=float, default=1.0, help="Seconds between round trips")
    parser.add_argument("--max-equity-drop-pct", type=float, default=10.0,
                        help="Abort if equity falls this far below the start")
    parser.add_argument("--traded-so-far", type=float, default=None,
                        help="Your lifetime volume, to report progress toward the $100k gate")
    parser.add_argument("--execute", action="store_true",
                        help="Actually place orders. Without this it is a dry run.")
    parser.add_argument("--allow-mainnet", action="store_true",
                        help="Required to run against mainnet, where fees are real")
    parser.add_argument("--allow-bot-coin", action="store_true",
                        help="Allow churning a coin listed in SYMBOLS (dangerous)")
    args = parser.parse_args()

    settings = load_settings()
    adapter = ExchangeAdapter(settings)
    symbol = args.coin if "/" in args.coin else f"{args.coin.upper()}/USDC:USDC"

    try:
        plan = plan_farm(settings, adapter, symbol, args.target, args.leverage, args.max_notional)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    remaining = None
    if args.traded_so_far is not None:
        remaining = max(0.0, SUBACCOUNT_VOLUME_REQUIREMENT - args.traded_so_far)
    _print_plan(plan, settings, remaining)

    problems = check_collisions(settings, adapter, symbol, args.allow_bot_coin)
    if problems:
        print("Refusing to run:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)

    if not settings.testnet and not args.allow_mainnet:
        print(
            "Refusing to run on MAINNET without --allow-mainnet.\n"
            f"This would spend ~${plan.estimated_fees:,.2f} of real USDC on fees for no return.\n"
            "Note your bot already generates volume by trading normally, and a second\n"
            "independent wallet has no volume gate at all."
        )
        sys.exit(1)

    if not args.execute:
        print("Dry run. Re-run with --execute to place orders.")
        return

    before = today_volume(settings)
    print(f"Starting. Volume traded today before this run: "
          f"{'unknown' if before is None else f'${before:,.2f}'}")

    farmer = VolumeFarmer(
        settings,
        adapter,
        pause=args.pause,
        max_equity_drop_pct=args.max_equity_drop_pct,
    )
    result = farmer.run(plan)

    print()
    print("=" * 66)
    print(f"  Generated:   ${result.volume:,.2f} across {result.trips} round trips")
    print(f"  Fees paid:   ${result.fees:,.2f}")
    print(f"  Trade P&L:   ${result.realized_pnl:+,.2f} (should be near zero)")
    print(f"  Net cost:    ${result.fees - result.realized_pnl:,.2f}")
    if result.aborted:
        print(f"  ABORTED:     {result.aborted}")
    after = today_volume(settings)
    if before is not None and after is not None:
        print(f"  Exchange-side volume today: ${before:,.2f} -> ${after:,.2f}")
    print("=" * 66)
    sys.exit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
