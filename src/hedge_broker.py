"""Execution and state machine for the manual catalyst hedge.

``HedgeManager.poll()`` is called once per bot loop. It drives the record in
``logs/hedge.json`` through its lifecycle:

    requested -> open -> cut -> closed

* **requested** — a Telegram ``/hedge arm BTC`` wrote the request. Size both
  legs off the *smaller* of the two account equities so they can open at equal
  size, then open long in the main account and short in the sub-account.
* **open** — both legs live, net exposure ~zero. Each leg carries a stop at
  ``HEDGE_STOP_ATR_MULT`` reference ATRs from entry. The first stop touched
  identifies the loser: it closes for a capped loss and the survivor is
  promoted to winner. This *is* the "let incoming direction pick the side"
  behaviour; long and short stops sit on opposite sides of entry, so exactly
  one can trigger at any price.
* **cut** — the winner trails at ``HEDGE_TRAIL_ATR_MULT`` reference ATRs from
  its best price and exits when that trail is touched.

Execution goes through ``LegExecutor``, a thin seam over ``ExchangeAdapter`` so
the whole state machine can be tested without a network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional, Protocol

from . import hedge
from .config import Settings
from .exchange import ExchangeAdapter
from .hedge import (
    CLOSED,
    CUT,
    EXPIRED,
    FAILED,
    LONG,
    OPEN,
    REQUESTED,
    SHORT,
    HedgeLeg,
    HedgeState,
)

MAIN = "main"
SUB = "sub"


class Executor(Protocol):
    """What the hedge needs from an account to trade it."""

    def equity(self) -> float: ...
    def round_size(self, symbol: str, size: float) -> float: ...
    def open(self, symbol: str, side: str, size: float, ref_price: float) -> tuple[float, float]: ...
    def close(self, symbol: str, size: float, ref_price: float) -> tuple[float, float]: ...


class LegExecutor:
    """Adapter-backed executor for one account (main or sub)."""

    def __init__(self, adapter: ExchangeAdapter, account: str) -> None:
        self.adapter = adapter
        self.account = account

    def equity(self) -> float:
        return float(self.adapter.fetch_balance())

    def round_size(self, symbol: str, size: float) -> float:
        return float(self.adapter.round_size(symbol, size))

    def open(self, symbol: str, side: str, size: float, ref_price: float) -> tuple[float, float]:
        resp = self.adapter.create_market_order(symbol, side, size, params={})
        fill = float(resp.get("average") or ref_price)
        filled = float(resp.get("filled") or size)
        return fill, filled

    def close(self, symbol: str, size: float, ref_price: float) -> tuple[float, float]:
        # reduceOnly routes to market_close, which resolves the open position in
        # whichever account this executor points at.
        resp = self.adapter.create_market_order(
            symbol, "sell", size, params={"reduceOnly": True}
        )
        fill = float(resp.get("average") or ref_price)
        filled = float(resp.get("filled") or size)
        return fill, filled


class HedgeManager:
    """Drives one hedge at a time. Safe to construct even when unconfigured."""

    def __init__(
        self,
        settings: Settings,
        main: Optional[Executor] = None,
        sub: Optional[Executor] = None,
        log_event: Optional[Callable[[str, dict], None]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.settings = settings
        self.main = main
        self.sub = sub
        self._log = log_event or (lambda event, payload: None)
        self._now = now or (lambda: datetime.now(timezone.utc))

    # -- helpers -----------------------------------------------------------

    @property
    def logs_dir(self) -> str:
        return self.settings.logs_dir

    def _iso(self) -> str:
        return self._now().isoformat()

    def _save(self, state: HedgeState) -> HedgeState:
        return hedge.write_hedge(self.logs_dir, state)

    def _fee(self, notional: float, bps: float) -> float:
        return abs(notional) * (bps / 10_000)

    def _executor(self, account: str) -> Optional[Executor]:
        return self.main if account == MAIN else self.sub

    def _ready(self) -> bool:
        return bool(self.settings.hedge_configured and self.main and self.sub)

    # -- sizing ------------------------------------------------------------

    def plan_size(self, ref_price: float, ref_atr: float) -> tuple[float, str]:
        """Equal size for both legs, or ``(0, reason)`` when it cannot be sized.

        Risk is a fraction of *combined* equity, but the leverage cap uses the
        *smaller* account: both legs must be openable at the same size, and the
        short leg only has the sub-account's margin behind it.
        """
        if ref_price <= 0 or ref_atr <= 0:
            return 0.0, "no price or ATR"
        assert self.main and self.sub

        main_eq = self.main.equity()
        sub_eq = self.sub.equity()
        if main_eq <= 0 or sub_eq <= 0:
            return 0.0, f"an account has no equity (main={main_eq:.2f}, sub={sub_eq:.2f})"

        stop_distance = self.settings.hedge_stop_atr_mult * ref_atr
        if stop_distance <= 0:
            return 0.0, "stop distance is zero"

        risk_amount = (main_eq + sub_eq) * (self.settings.hedge_risk_pct / 100.0)
        size = risk_amount / stop_distance

        cap = (min(main_eq, sub_eq) * self.settings.max_leverage) / ref_price
        if size > cap:
            size = cap
        return max(size, 0.0), ""

    # -- lifecycle steps ---------------------------------------------------

    def poll(
        self,
        fetch_candles: Callable[[str], list],
        fetch_price: Callable[[str], float],
    ) -> Optional[HedgeState]:
        """Advance the active hedge by one step. Returns the state it left it in."""
        if not self.settings.hedge_enabled:
            return None

        state = hedge.read_hedge(self.logs_dir)
        if state is None or not state.is_active:
            return None

        if state.expired(self.settings.hedge_expiry_hours, self._now()):
            state.state = EXPIRED
            state.closed_at = self._iso()
            state.error = "request expired before it could be opened"
            self._log("hedge_expired", {"symbol": state.symbol})
            return self._save(state)

        if not self._ready():
            state.state = FAILED
            state.closed_at = self._iso()
            state.error = "hedge is not configured (need HEDGE_SUB_ACCOUNT and both accounts)"
            self._log("hedge_failed", {"symbol": state.symbol, "error": state.error})
            return self._save(state)

        try:
            price = float(fetch_price(state.symbol))
        except Exception as exc:  # noqa: BLE001
            self._log("hedge_error", {"symbol": state.symbol, "error": f"price: {exc}"})
            return state

        if state.state == REQUESTED:
            return self._open_pair(state, fetch_candles, price)

        manual = state.cut_reason.startswith("manual_close")
        if manual or state.stale(self.settings.hedge_max_hours, self._now()):
            reason = state.cut_reason if manual else "hedge_max_hours"
            return self._flatten(state, price, reason)

        if state.state == OPEN:
            return self._check_stops(state, price)
        if state.state == CUT:
            return self._trail_winner(state, price)
        return state

    def _open_pair(
        self,
        state: HedgeState,
        fetch_candles: Callable[[str], list],
        price: float,
    ) -> HedgeState:
        try:
            candles = fetch_candles(state.symbol)
        except Exception as exc:  # noqa: BLE001
            self._log("hedge_error", {"symbol": state.symbol, "error": f"candles: {exc}"})
            return state

        ref_atr = hedge.reference_atr(
            candles,
            self.settings.atr_period,
            self.settings.hedge_atr_floor_pct,
        )
        size, why = self.plan_size(price, ref_atr)
        if size <= 0:
            state.state = FAILED
            state.closed_at = self._iso()
            state.error = f"cannot size the hedge: {why or 'size rounds to zero'}"
            self._log("hedge_failed", {"symbol": state.symbol, "error": state.error})
            return self._save(state)

        assert self.main and self.sub
        size = min(
            self.main.round_size(state.symbol, size),
            self.sub.round_size(state.symbol, size),
        )
        if size <= 0:
            state.state = FAILED
            state.closed_at = self._iso()
            state.error = "size rounds to zero on one of the accounts"
            self._log("hedge_failed", {"symbol": state.symbol, "error": state.error})
            return self._save(state)

        state.ref_price = price
        state.ref_atr = ref_atr
        state.atr_pct = hedge.atr_percentile(candles, self.settings.atr_period)
        stop_distance = self.settings.hedge_stop_atr_mult * ref_atr

        opened: list[HedgeLeg] = []
        for direction, account in ((LONG, MAIN), (SHORT, SUB)):
            executor = self._executor(account)
            assert executor
            leg = HedgeLeg(direction=direction, account=account)
            try:
                fill, filled = executor.open(state.symbol, leg.side, size, price)
            except Exception as exc:  # noqa: BLE001
                state.error = f"{direction} leg failed to open: {exc}"
                self._log(
                    "hedge_failed",
                    {"symbol": state.symbol, "leg": direction, "error": str(exc)},
                )
                self._unwind(state, opened, price)
                state.state = FAILED
                state.closed_at = self._iso()
                return self._save(state)

            leg.size = filled
            leg.entry_price = fill
            leg.peak_price = fill
            leg.opened_at = self._iso()
            leg.stop_price = (
                fill - stop_distance if direction == LONG else fill + stop_distance
            )
            state.legs[direction] = leg
            opened.append(leg)

        state.state = OPEN
        state.opened_at = self._iso()
        self._log(
            "hedge_open",
            {
                "symbol": state.symbol,
                "size": size,
                "ref_price": price,
                "ref_atr": ref_atr,
                "atr_percentile": state.atr_pct,
                "long_stop": state.legs[LONG].stop_price,
                "short_stop": state.legs[SHORT].stop_price,
                "note": state.note,
            },
        )
        return self._save(state)

    def _unwind(self, state: HedgeState, opened: list[HedgeLeg], price: float) -> None:
        """Best-effort close of legs that filled before a sibling failed.

        A half-open hedge is an unintended naked directional position, so this
        runs even if it has to swallow errors — the record keeps the evidence.
        """
        for leg in opened:
            executor = self._executor(leg.account)
            if executor is None or not leg.is_open:
                continue
            try:
                fill, _ = executor.close(state.symbol, leg.size, price)
                self._close_leg(state, leg, fill, "unwind_failed_open")
            except Exception as exc:  # noqa: BLE001
                state.error = f"{state.error}; unwind of {leg.direction} failed: {exc}"
                self._log(
                    "hedge_unwind_failed",
                    {"symbol": state.symbol, "leg": leg.direction, "error": str(exc)},
                )

    def _close_leg(self, state: HedgeState, leg: HedgeLeg, exit_price: float, reason: str) -> float:
        """Record a leg as closed and return its realized P&L net of fees."""
        raw = (exit_price - leg.entry_price) * leg.size
        if leg.direction == SHORT:
            raw = -raw
        fees = self._fee(leg.entry_price * leg.size, self.settings.entry_fee_bps) + self._fee(
            exit_price * leg.size, self.settings.exit_fee_bps
        )
        leg.exit_price = exit_price
        leg.exit_reason = reason
        leg.closed_at = self._iso()
        leg.realized_pnl = raw - fees
        self._log(
            "hedge_leg_close",
            {
                "symbol": state.symbol,
                "leg": leg.direction,
                "account": leg.account,
                "entry": leg.entry_price,
                "exit": exit_price,
                "size": leg.size,
                "reason": reason,
                "pnl": leg.realized_pnl,
            },
        )
        return leg.realized_pnl

    def _check_stops(self, state: HedgeState, price: float) -> HedgeState:
        """Whichever stop the market reaches first identifies the losing leg.

        Long and short stops sit on opposite sides of entry, so at most one can
        be breached by a single price — the outcome is unambiguous.
        """
        long_leg = state.leg(LONG)
        short_leg = state.leg(SHORT)
        if not (long_leg and short_leg and long_leg.is_open and short_leg.is_open):
            # Legs out of sync with the state machine; flatten rather than guess.
            return self._flatten(state, price, "desynced_legs")

        loser: Optional[HedgeLeg] = None
        if long_leg.stop_price > 0 and price <= long_leg.stop_price:
            loser = long_leg
        elif short_leg.stop_price > 0 and price >= short_leg.stop_price:
            loser = short_leg
        if loser is None:
            return state

        executor = self._executor(loser.account)
        assert executor
        try:
            fill, _ = executor.close(state.symbol, loser.size, loser.stop_price)
        except Exception as exc:  # noqa: BLE001
            state.error = f"failed to cut {loser.direction} leg: {exc}"
            self._log(
                "hedge_error",
                {"symbol": state.symbol, "leg": loser.direction, "error": str(exc)},
            )
            return self._save(state)

        self._close_leg(state, loser, fill, "cut_loser")
        winner = SHORT if loser.direction == LONG else LONG
        state.winner = winner
        state.state = CUT
        state.cut_price = fill
        state.cut_at = self._iso()
        state.cut_reason = "stop_hit"
        # Re-anchor the winner's trail to the best price seen so far.
        winner_leg = state.legs[winner]
        winner_leg.peak_price = (
            max(winner_leg.peak_price, price) if winner == LONG else min(winner_leg.peak_price, price)
        )
        self._apply_trail(winner_leg, state.ref_atr)
        self._log(
            "hedge_cut",
            {
                "symbol": state.symbol,
                "loser": loser.direction,
                "winner": winner,
                "cut_price": fill,
                "loser_pnl": loser.realized_pnl,
                "winner_stop": winner_leg.stop_price,
            },
        )
        return self._save(state)

    def _apply_trail(self, leg: HedgeLeg, ref_atr: float) -> None:
        """Ratchet the winner's stop toward its best price; never loosen it."""
        distance = self.settings.hedge_trail_atr_mult * ref_atr
        if distance <= 0 or leg.peak_price <= 0:
            return
        if leg.direction == LONG:
            leg.stop_price = max(leg.stop_price, leg.peak_price - distance)
        else:
            candidate = leg.peak_price + distance
            leg.stop_price = candidate if leg.stop_price <= 0 else min(leg.stop_price, candidate)

    def _trail_winner(self, state: HedgeState, price: float) -> HedgeState:
        winner = state.winner
        leg = state.leg(winner) if winner else None
        if leg is None or not leg.is_open:
            state.state = CLOSED
            state.closed_at = self._iso()
            self._log("hedge_closed", {"symbol": state.symbol, "pnl": state.realized_pnl})
            return self._save(state)

        leg.peak_price = (
            max(leg.peak_price, price) if leg.direction == LONG else min(leg.peak_price, price)
        )
        self._apply_trail(leg, state.ref_atr)

        hit = leg.stop_price > 0 and (
            price <= leg.stop_price if leg.direction == LONG else price >= leg.stop_price
        )
        if not hit:
            return self._save(state)

        executor = self._executor(leg.account)
        assert executor
        try:
            fill, _ = executor.close(state.symbol, leg.size, leg.stop_price)
        except Exception as exc:  # noqa: BLE001
            state.error = f"failed to close winner: {exc}"
            self._log("hedge_error", {"symbol": state.symbol, "error": state.error})
            return self._save(state)

        self._close_leg(state, leg, fill, "trail")
        state.state = CLOSED
        state.closed_at = self._iso()
        self._log(
            "hedge_closed",
            {
                "symbol": state.symbol,
                "winner": winner,
                "winner_pnl": leg.realized_pnl,
                "total_pnl": state.realized_pnl,
            },
        )
        return self._save(state)

    def _flatten(self, state: HedgeState, price: float, reason: str) -> HedgeState:
        """Close every open leg at market and finish the hedge."""
        for leg in list(state.legs.values()):
            if not leg.is_open:
                continue
            executor = self._executor(leg.account)
            if executor is None:
                continue
            try:
                fill, _ = executor.close(state.symbol, leg.size, price)
            except Exception as exc:  # noqa: BLE001
                state.error = f"{state.error}; failed to close {leg.direction}: {exc}"
                self._log(
                    "hedge_error",
                    {"symbol": state.symbol, "leg": leg.direction, "error": str(exc)},
                )
                return self._save(state)
            self._close_leg(state, leg, fill, reason)

        state.state = CLOSED
        state.closed_at = self._iso()
        self._log(
            "hedge_closed",
            {"symbol": state.symbol, "reason": reason, "total_pnl": state.realized_pnl},
        )
        return self._save(state)


def build_manager(
    settings: Settings,
    log_event: Optional[Callable[[str, dict], None]] = None,
) -> Optional[HedgeManager]:
    """A live manager, or None when the hedge is off or misconfigured.

    Import is local so ``src.subaccount`` (and the SDK objects it builds) are
    only touched when the hedge is actually enabled.
    """
    if not settings.hedge_configured:
        return None
    from .subaccount import build_adapters

    main_adapter, sub_adapter = build_adapters(settings)
    return HedgeManager(
        settings,
        main=LegExecutor(main_adapter, MAIN),
        sub=LegExecutor(sub_adapter, SUB),
        log_event=log_event,
    )
