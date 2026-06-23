"""Hyperliquid exchange adapter (official ``hyperliquid-python-sdk``) and brokers.

``ExchangeAdapter`` wraps ``Info`` for market data and ``Exchange`` for signed
actions when ``HL_PRIVATE_KEY`` is set. ``PaperBroker`` simulates fills;
``LiveBroker`` places real orders.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

from eth_account import Account
from hyperliquid.exchange import Exchange as HLExchange
from hyperliquid.info import Info
from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL

from .config import Settings
from .models import Leg, PendingOrder, Position, Side, TradeResult

T = TypeVar("T")


def hl_coin(symbol: str) -> str:
    """Map ``BTC/USDC:USDC`` or ``BTC`` to Hyperliquid perp coin ``BTC``."""
    s = symbol.strip()
    if "/" in s:
        return s.split("/")[0]
    return s


def _timeframe_ms(timeframe: str) -> int:
    tf = timeframe.strip().lower()
    if tf.endswith("m"):
        return int(tf[:-1]) * 60_000
    if tf.endswith("h"):
        return int(tf[:-1]) * 3_600_000
    if tf.endswith("d"):
        return int(tf[:-1]) * 86_400_000
    return 300_000


def _candles_to_rows(raw: List[dict[str, Any]]) -> List[List[float]]:
    rows: List[List[float]] = []
    for c in sorted(raw, key=lambda x: int(x["t"])):
        rows.append(
            [
                int(c["t"]),
                float(c["o"]),
                float(c["h"]),
                float(c["l"]),
                float(c["c"]),
                float(c["v"]),
            ]
        )
    return rows


class ExchangeAdapter:
    """Hyperliquid via official SDK: ``Info`` + optional signed ``Exchange``."""

    MAX_RETRIES = 3
    RETRY_DELAY = 2.0

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        base = TESTNET_API_URL if settings.testnet else MAINNET_API_URL
        self._info = Info(base_url=base, skip_ws=True, timeout=15.0)
        self._sz_decimals: Optional[Dict[str, int]] = None
        self._hlx: Optional[HLExchange] = None
        if settings.private_key:
            wallet = Account.from_key(settings.private_key)
            addr = settings.wallet_address.strip()
            if addr and addr.lower() != wallet.address.lower():
                self._hlx = HLExchange(
                    wallet,
                    base_url=base,
                    account_address=addr,
                    timeout=15.0,
                )
            else:
                self._hlx = HLExchange(wallet, base_url=base, timeout=15.0)

    def _retry(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        last_exc: Exception | None = None
        for attempt in range(self.MAX_RETRIES):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_DELAY)
        raise last_exc  # type: ignore[misc]

    def _require_exchange(self) -> HLExchange:
        if self._hlx is None:
            raise RuntimeError("Signing requires HL_PRIVATE_KEY in .env")
        return self._hlx

    def _asset_sz_decimals(self) -> Dict[str, int]:
        """Per-coin ``szDecimals`` from the perp ``meta`` (fetched once, cached)."""
        if self._sz_decimals is None:
            meta = self._retry(lambda: self._info.meta())
            self._sz_decimals = {
                a["name"]: int(a["szDecimals"])
                for a in meta.get("universe", [])
                if "name" in a and "szDecimals" in a
            }
        return self._sz_decimals

    def round_size(self, symbol: str, size: float) -> float:
        """Floor ``size`` to the asset's ``szDecimals``.

        Hyperliquid rejects orders whose size carries more precision than the
        asset allows (the SDK raises ``float_to_wire causes rounding``). We
        floor (never round up) so the position can't exceed the risk-based
        size. Returns 0.0 if the size floors below the smallest increment.
        """
        coin = hl_coin(symbol)
        decimals = self._asset_sz_decimals().get(coin)
        if decimals is None:
            return size
        factor = 10 ** decimals
        return math.floor(size * factor) / factor

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> List[List[float]]:
        coin = hl_coin(symbol)
        interval_ms = _timeframe_ms(timeframe)
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - (limit + 2) * interval_ms

        def _fetch() -> List[dict[str, Any]]:
            return self._info.candles_snapshot(coin, timeframe, start_ms, end_ms)

        raw = self._retry(_fetch)
        rows = _candles_to_rows(raw)
        if len(rows) > limit:
            rows = rows[-limit:]
        return rows

    def fetch_last_price(self, symbol: str) -> float:
        coin = hl_coin(symbol)

        def _mid() -> dict[str, Any]:
            return self._info.all_mids()

        mids = self._retry(_mid)
        return float(mids[coin])

    def fetch_balance(self) -> float:
        """Total USDC equity available for sizing.

        On a Hyperliquid **unified account**, USDC sits in the spot wallet and is
        only pulled into the perps wallet as margin when a position is opened, so
        the perps ``accountValue`` reflects just the *committed* margin (and is 0
        while flat). For position sizing we want the whole bankroll, so we return
        perps ``accountValue`` (includes open-position margin + unrealized PnL)
        **plus** the free spot USDC balance.
        """
        addr = self.settings.wallet_address.strip()
        if not addr:
            return self.settings.initial_equity

        def _state() -> dict[str, Any]:
            return self._info.user_state(addr)

        st = self._retry(_state)
        perps = float(st["marginSummary"]["accountValue"])

        spot_usdc = 0.0
        try:
            sp = self._retry(lambda: self._info.spot_user_state(addr))
            for bal in sp.get("balances", []) or []:
                if str(bal.get("coin", "")).upper() == "USDC":
                    spot_usdc = float(bal.get("total", 0) or 0)
                    break
        except Exception:  # noqa: BLE001
            pass  # spot lookup is best-effort; fall back to perps-only

        return perps + spot_usdc

    def fetch_positions(self) -> List[dict[str, Any]]:
        """Open perp positions from the live account.

        Returns one dict per non-zero position:
        ``{"coin", "side", "size", "entry_price", "unrealized_pnl"}`` where
        ``size`` is absolute and ``side`` is ``"buy"`` (long) / ``"sell"`` (short).
        Empty list if no wallet is configured or nothing is open.
        """
        addr = self.settings.wallet_address.strip()
        if not addr:
            return []

        def _state() -> dict[str, Any]:
            return self._info.user_state(addr)

        st = self._retry(_state)
        out: List[dict[str, Any]] = []
        for ap in st.get("assetPositions", []) or []:
            pos = ap.get("position") or {}
            szi = float(pos.get("szi", 0) or 0)
            if szi == 0:
                continue
            out.append(
                {
                    "coin": str(pos.get("coin", "")),
                    "side": "buy" if szi > 0 else "sell",
                    "size": abs(szi),
                    "entry_price": float(pos.get("entryPx", 0) or 0),
                    "unrealized_pnl": float(pos.get("unrealizedPnl", 0) or 0),
                }
            )
        return out

    @staticmethod
    def _normalize_bulk_statuses(result: dict[str, Any]) -> dict[str, Any]:
        if result.get("status") != "ok":
            raise RuntimeError(result)
        statuses = result.get("response", {}).get("data", {}).get("statuses") or []
        if not statuses:
            raise RuntimeError("empty order statuses")
        st0 = statuses[0]
        if "error" in st0:
            raise RuntimeError(st0["error"])
        if "resting" in st0:
            oid = st0["resting"]["oid"]
            return {"id": str(oid), "status": "open", "filled": 0.0, "average": None}
        if "filled" in st0:
            f = st0["filled"]
            return {
                "id": str(f["oid"]),
                "status": "closed",
                "filled": float(f["totalSz"]),
                "average": float(f["avgPx"]),
            }
        raise RuntimeError(st0)

    def create_limit_order(
        self, symbol: str, side: str, amount: float, price: float
    ) -> dict:
        x = self._require_exchange()
        coin = hl_coin(symbol)
        is_buy = side == "buy"
        amount = self.round_size(symbol, amount)
        if amount <= 0:
            raise RuntimeError(f"order size rounds to zero for {coin}")

        def _place() -> dict[str, Any]:
            return x.order(
                coin,
                is_buy,
                amount,
                price,
                {"limit": {"tif": "Gtc"}},
                reduce_only=False,
            )

        raw = self._retry(_place)
        return self._normalize_bulk_statuses(raw)

    def create_market_order(
        self, symbol: str, side: str, amount: float, params: Optional[dict] = None
    ) -> dict:
        x = self._require_exchange()
        coin = hl_coin(symbol)
        params = params or {}
        reduce_only = bool(params.get("reduceOnly"))
        amount = self.round_size(symbol, amount)
        if amount <= 0:
            raise RuntimeError(f"order size rounds to zero for {coin}")

        def _go() -> dict[str, Any]:
            if reduce_only:
                return x.market_close(coin, sz=amount, slippage=0.02)
            is_buy = side == "buy"
            return x.market_open(coin, is_buy, amount, None, slippage=0.02)

        raw = self._retry(_go)
        return self._normalize_bulk_statuses(raw)

    def fetch_order(self, order_id: str, symbol: str) -> dict:
        addr = self.settings.wallet_address.strip()
        if not addr:
            raise RuntimeError("HL_WALLET_ADDRESS required for order lookup")

        def _q() -> dict[str, Any]:
            return self._info.query_order_by_oid(addr, int(order_id))

        raw = self._retry(_q)
        return self._normalize_query_order(raw, order_id)

    @staticmethod
    def _normalize_query_order(raw: dict[str, Any], order_id: str) -> dict[str, Any]:
        if raw.get("status") == "unknownOid":
            return {
                "id": order_id,
                "status": "canceled",
                "average": None,
                "filled": 0.0,
            }
        if raw.get("status") != "order":
            return {"id": order_id, "status": "open", "average": None, "filled": 0.0}
        order = raw["order"]
        detail = order.get("order", order)
        st = str(detail.get("status", "")).lower()
        oid = detail.get("oid", order_id)
        if st == "filled":
            avg = detail.get("avgPx")
            if avg is None:
                avg = detail.get("limitPx", 0)
            filled_sz = detail.get("origSz", detail.get("sz", 0))
            return {
                "id": str(oid),
                "status": "closed",
                "average": float(avg),
                "filled": float(filled_sz),
            }
        if st in ("canceled", "cancelled", "rejected"):
            return {
                "id": str(oid),
                "status": "canceled",
                "average": None,
                "filled": 0.0,
            }
        return {"id": str(oid), "status": "open", "average": None, "filled": 0.0}

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        x = self._require_exchange()
        coin = hl_coin(symbol)

        def _cx() -> dict[str, Any]:
            return x.cancel(coin, int(order_id))

        return self._retry(_cx)

    def set_leverage(self, leverage: int, symbol: str) -> None:
        if self._hlx is None:
            return
        coin = hl_coin(symbol)
        x = self._hlx

        def _lev() -> Any:
            return x.update_leverage(leverage, coin, True)

        try:
            self._retry(_lev)
        except Exception:  # noqa: BLE001
            pass


class _BrokerBase:
    """Shared helpers for both Paper and Live brokers."""

    def __init__(self, settings: Settings, exchange: ExchangeAdapter) -> None:
        self.settings = settings
        self.exchange = exchange
        self.equity: float = settings.initial_equity
        self.positions: Dict[str, Position] = {}
        self.pending_orders: Dict[str, PendingOrder] = {}
        self.logs_dir = Path(settings.logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _write_jsonl(self, name: str, payload: dict) -> None:
        path = self.logs_dir / name
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")

    def log_event(self, event: str, payload: dict) -> None:
        record = {"ts": self._now().isoformat(), "event": event}
        record.update(payload)
        self._write_jsonl("events.jsonl", record)

    def _entry_fee(self, notional: float) -> float:
        return notional * (self.settings.entry_fee_bps / 10_000)

    def _exit_fee(self, notional: float) -> float:
        return notional * (self.settings.exit_fee_bps / 10_000)

    def cancel_pending(self, symbol: str) -> bool:
        if symbol in self.pending_orders:
            del self.pending_orders[symbol]
            self.log_event("limit_order_cancelled", {"symbol": symbol})
            return True
        return False

    def is_pending_expired(self, symbol: str) -> bool:
        order = self.pending_orders.get(symbol)
        if not order:
            return False
        elapsed = (self._now() - order.placed_at).total_seconds()
        return elapsed >= self.settings.limit_timeout_seconds


class PaperBroker(_BrokerBase):
    """Simulated execution engine: equity tracking, limit fills, fee/slippage, and JSONL logging."""

    # ------------------------------------------------------------------
    # Limit entry flow
    # ------------------------------------------------------------------

    def place_limit_entry(
        self,
        symbol: str,
        side: Side,
        size: float,
        limit_price: float,
        stop_price: float,
        take_profit_price: float,
        initial_stop_distance: float = 0.0,
        trail_atr: float = 0.0,
    ) -> Optional[PendingOrder]:
        """Queue a limit entry.  Returns None if size<=0, already positioned, or pending."""
        if size <= 0 or symbol in self.positions or symbol in self.pending_orders:
            return None
        order = PendingOrder(
            symbol=symbol,
            side=side,
            size=size,
            limit_price=limit_price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            placed_at=self._now(),
            initial_stop_distance=initial_stop_distance,
            trail_atr=trail_atr,
        )
        self.pending_orders[symbol] = order
        self.log_event(
            "limit_order_placed",
            {
                "symbol": symbol,
                "side": side,
                "size": size,
                "limit_price": limit_price,
                "stop_price": stop_price,
                "take_profit_price": take_profit_price,
            },
        )
        return order

    def check_pending_fill(
        self,
        symbol: str,
        known_price: Optional[float] = None,
    ) -> Optional[Position]:
        """Check if a pending limit order would fill at current price."""
        order = self.pending_orders.get(symbol)
        if not order:
            return None

        last = (
            known_price
            if known_price is not None
            else self.exchange.fetch_last_price(symbol)
        )
        filled = False
        if order.side == "buy" and last <= order.limit_price:
            filled = True
        elif order.side == "sell" and last >= order.limit_price:
            filled = True

        if not filled:
            return None

        entry = order.limit_price
        notional = entry * order.size
        fee = self._entry_fee(notional)
        self.equity -= fee

        pos = Position(
            symbol=symbol,
            side=order.side,
            size=order.size,
            entry_price=entry,
            stop_price=order.stop_price,
            take_profit_price=order.take_profit_price,
            opened_at=self._now(),
            initial_stop_distance=order.initial_stop_distance,
            trail_atr=order.trail_atr,
            peak_price=entry,
        )
        self.positions[symbol] = pos
        del self.pending_orders[symbol]

        self.log_event(
            "position_open",
            {
                "symbol": symbol,
                "side": order.side,
                "size": order.size,
                "entry_price": entry,
                "stop_price": order.stop_price,
                "take_profit_price": order.take_profit_price,
                "fee": fee,
                "equity": self.equity,
                "order_type": "limit",
            },
        )
        return pos

    # ------------------------------------------------------------------
    # DCA leg flow (new strategy entry point)
    # ------------------------------------------------------------------

    def open_leg(
        self,
        symbol: str,
        side: Side,
        size: float,
        fill_price: float,
    ) -> Optional[Position]:
        """Add one leg to a (new or existing) composite position.

        Paper-mode fills are immediate at ``fill_price`` with entry slippage applied.
        Recomputes the composite ``size`` (sum) and ``entry_price``
        (size-weighted average) after the leg is appended.  Returns the
        updated ``Position``, or None if size is non-positive.
        """
        if size <= 0:
            return None

        slip = self.settings.slippage_bps / 10_000
        if side == "buy":
            entry_px = fill_price * (1 + slip)
        else:
            entry_px = fill_price * (1 - slip)

        notional = entry_px * size
        fee = self._entry_fee(notional)
        self.equity -= fee

        leg = Leg(
            size=size,
            entry_price=entry_px,
            opened_at=self._now(),
            fee=fee,
        )

        pos = self.positions.get(symbol)
        if pos is None:
            pos = Position(
                symbol=symbol,
                side=side,
                size=size,
                entry_price=entry_px,
                opened_at=self._now(),
                legs=[leg],
                last_fill_price=entry_px,
                peak_price=entry_px,
            )
            self.positions[symbol] = pos
        else:
            pos.legs.append(leg)
            total_size = sum(l.size for l in pos.legs)
            weighted = sum(l.size * l.entry_price for l in pos.legs) / total_size
            pos.size = total_size
            pos.entry_price = weighted
            pos.last_fill_price = entry_px

        self.log_event(
            "leg_open",
            {
                "symbol": symbol,
                "side": side,
                "leg_index": len(pos.legs),
                "leg_size": size,
                "leg_entry_price": entry_px,
                "avg_entry_price": pos.entry_price,
                "total_size": pos.size,
                "fee": fee,
                "equity": self.equity,
            },
        )
        return pos

    def close_all_legs(
        self,
        symbol: str,
        exit_at: float,
        exit_reason: str,
    ) -> Optional[TradeResult]:
        """Close every leg of a composite position at once.

        Computes net P/L as ``(exit_px - avg_entry) * total_size - exit_fee``
        (long-only, but signed correctly for shorts in case of future use).
        """
        pos = self.positions.get(symbol)
        if not pos:
            return None

        exit_side: Side = "sell" if pos.side == "buy" else "buy"
        slip = self.settings.slippage_bps / 10_000
        if exit_side == "buy":
            exit_px = exit_at * (1 + slip)
        else:
            exit_px = exit_at * (1 - slip)

        total_size = pos.size
        notional = exit_px * total_size
        fee = self._exit_fee(notional)

        raw_pnl = (exit_px - pos.entry_price) * total_size
        if pos.side == "sell":
            raw_pnl = -raw_pnl
        net_pnl = raw_pnl - fee
        self.equity += net_pnl

        trade = TradeResult(
            symbol=symbol,
            side=pos.side,
            size=total_size,
            entry_price=pos.entry_price,
            exit_price=exit_px,
            pnl=net_pnl,
            fees=fee + sum(l.fee for l in pos.legs),
            opened_at=pos.opened_at,
            closed_at=self._now(),
            exit_reason=exit_reason,
            legs=len(pos.legs),
        )
        del self.positions[symbol]
        payload = asdict(trade)
        payload["opened_at"] = trade.opened_at.isoformat()
        payload["closed_at"] = trade.closed_at.isoformat()
        payload["equity"] = self.equity
        self._write_jsonl("trades.jsonl", payload)
        self.log_event(
            "position_close",
            {
                "symbol": trade.symbol,
                "side": trade.side,
                "size": trade.size,
                "legs": trade.legs,
                "avg_entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "pnl": trade.pnl,
                "fees": trade.fees,
                "equity": self.equity,
                "exit_reason": exit_reason,
            },
        )
        return trade

    # ------------------------------------------------------------------
    # Close at exact price level (legacy single-leg exit, kept for compat)
    # ------------------------------------------------------------------

    def close_position_at(
        self,
        symbol: str,
        exit_at: float,
        exit_reason: str,
    ) -> Optional[TradeResult]:
        """Close position at an exact stop/TP level with slippage applied.

        Deprecated in favour of ``close_all_legs`` under the DCA strategy.
        Forwards to ``close_all_legs`` so legacy callers keep working.
        """
        return self.close_all_legs(symbol, exit_at, exit_reason)


class LiveBroker(_BrokerBase):
    """Real execution engine: places orders via ``hyperliquid-python-sdk``.

    Same interface as ``PaperBroker`` so ``FuturesBot`` works unchanged.
    Entry = limit order on the exchange; exit = market order (reduceOnly).
    Equity is synced from the Hyperliquid account balance.
    """

    def __init__(self, settings: Settings, exchange: ExchangeAdapter) -> None:
        super().__init__(settings, exchange)
        self._sync_equity()
        self._reconcile_positions()
        for sym in settings.symbols:
            exchange.set_leverage(int(settings.max_leverage), sym)

    def _sync_equity(self) -> None:
        try:
            self.equity = self.exchange.fetch_balance()
        except Exception:  # noqa: BLE001
            pass  # keep last known equity

    def _reconcile_positions(self) -> None:
        """Adopt positions already open on Hyperliquid at startup.

        Without this, a restart (crash, deploy, reboot) leaves ``self.positions``
        empty while real exposure is open on the exchange — the bot would then
        open a *second* position and never manage the orphaned one. We rebuild a
        single-leg ``Position`` per live position so the strategy can resume
        managing its exit. Trail state (peak/stop) is intentionally reset and
        will re-ratchet from live candles on the next ticks.
        """
        try:
            live = self.exchange.fetch_positions()
        except Exception as exc:  # noqa: BLE001
            self.log_event("reconcile_error", {"error": str(exc)})
            return
        if not live:
            return

        # Map exchange coin (e.g. "BTC") back to a configured symbol.
        coin_to_symbol = {hl_coin(sym): sym for sym in self.settings.symbols}
        for lp in live:
            symbol = coin_to_symbol.get(lp["coin"])
            if symbol is None:
                self.log_event(
                    "reconcile_skip",
                    {"coin": lp["coin"], "reason": "not in configured SYMBOLS"},
                )
                continue
            entry = lp["entry_price"]
            size = lp["size"]
            leg = Leg(size=size, entry_price=entry, opened_at=self._now(), fee=0.0)
            self.positions[symbol] = Position(
                symbol=symbol,
                side=lp["side"],
                size=size,
                entry_price=entry,
                opened_at=self._now(),
                legs=[leg],
                last_fill_price=entry,
                peak_price=entry,
            )
            self.log_event(
                "position_reconciled",
                {
                    "symbol": symbol,
                    "side": lp["side"],
                    "size": size,
                    "entry_price": entry,
                    "unrealized_pnl": lp["unrealized_pnl"],
                },
            )
            print(
                f"[reconcile] adopted open {lp['side']} {symbol} "
                f"size={size} entry={entry} (uPnL={lp['unrealized_pnl']:+.2f})"
            )

    # ------------------------------------------------------------------
    # Limit entry flow (real orders)
    # ------------------------------------------------------------------

    def place_limit_entry(
        self,
        symbol: str,
        side: Side,
        size: float,
        limit_price: float,
        stop_price: float,
        take_profit_price: float,
        initial_stop_distance: float = 0.0,
        trail_atr: float = 0.0,
    ) -> Optional[PendingOrder]:
        if size <= 0 or symbol in self.positions or symbol in self.pending_orders:
            return None
        try:
            resp = self.exchange.create_limit_order(symbol, side, size, limit_price)
        except Exception as exc:
            self.log_event("order_error", {"symbol": symbol, "error": str(exc)})
            return None
        order = PendingOrder(
            symbol=symbol,
            side=side,
            size=size,
            limit_price=limit_price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            placed_at=self._now(),
            initial_stop_distance=initial_stop_distance,
            trail_atr=trail_atr,
            exchange_order_id=str(resp.get("id", "")),
        )
        self.pending_orders[symbol] = order
        self.log_event(
            "limit_order_placed",
            {
                "symbol": symbol,
                "side": side,
                "size": size,
                "limit_price": limit_price,
                "stop_price": stop_price,
                "take_profit_price": take_profit_price,
                "exchange_order_id": order.exchange_order_id,
            },
        )
        return order

    def check_pending_fill(
        self,
        symbol: str,
        known_price: Optional[float] = None,
    ) -> Optional[Position]:
        order = self.pending_orders.get(symbol)
        if not order or not order.exchange_order_id:
            return None
        try:
            status = self.exchange.fetch_order(order.exchange_order_id, symbol)
        except Exception:
            return None
        st = status.get("status")
        if st == "canceled":
            del self.pending_orders[symbol]
            self.log_event(
                "limit_order_gone",
                {"symbol": symbol, "exchange_order_id": order.exchange_order_id},
            )
            return None
        if st != "closed":
            return None

        fill_price = float(status.get("average", order.limit_price) or order.limit_price)
        self._sync_equity()

        pos = Position(
            symbol=symbol,
            side=order.side,
            size=float(status.get("filled", order.size) or order.size),
            entry_price=fill_price,
            stop_price=order.stop_price,
            take_profit_price=order.take_profit_price,
            opened_at=self._now(),
            initial_stop_distance=order.initial_stop_distance,
            trail_atr=order.trail_atr,
            peak_price=fill_price,
        )
        self.positions[symbol] = pos
        del self.pending_orders[symbol]

        self.log_event(
            "position_open",
            {
                "symbol": symbol,
                "side": pos.side,
                "size": pos.size,
                "entry_price": fill_price,
                "stop_price": order.stop_price,
                "take_profit_price": order.take_profit_price,
                "equity": self.equity,
                "order_type": "limit",
                "exchange_order_id": order.exchange_order_id,
            },
        )
        return pos

    def cancel_pending(self, symbol: str) -> bool:
        order = self.pending_orders.get(symbol)
        if not order:
            return False
        if order.exchange_order_id:
            try:
                self.exchange.cancel_order(order.exchange_order_id, symbol)
            except Exception as exc:
                self.log_event(
                    "cancel_error",
                    {"symbol": symbol, "error": str(exc)},
                )
        del self.pending_orders[symbol]
        self.log_event("limit_order_cancelled", {"symbol": symbol})
        return True

    # ------------------------------------------------------------------
    # DCA leg flow (new strategy entry point)
    # ------------------------------------------------------------------

    def open_leg(
        self,
        symbol: str,
        side: Side,
        size: float,
        fill_price: float,
    ) -> Optional[Position]:
        """Add one leg to a (new or existing) composite position via market order.

        ``fill_price`` is only used as a fallback if the exchange response
        doesn't include an average fill price.  Returns the updated Position,
        or None if the order errored or sized to zero.
        """
        if size <= 0:
            return None
        try:
            resp = self.exchange.create_market_order(symbol, side, size, params={})
        except Exception as exc:
            self.log_event("order_error", {"symbol": symbol, "error": str(exc)})
            return None

        entry_px = float(resp.get("average", fill_price) or fill_price)
        filled = float(resp.get("filled", size) or size)
        self._sync_equity()

        fee = self._entry_fee(entry_px * filled)
        leg = Leg(
            size=filled,
            entry_price=entry_px,
            opened_at=self._now(),
            fee=fee,
        )

        pos = self.positions.get(symbol)
        if pos is None:
            pos = Position(
                symbol=symbol,
                side=side,
                size=filled,
                entry_price=entry_px,
                opened_at=self._now(),
                legs=[leg],
                last_fill_price=entry_px,
                peak_price=entry_px,
            )
            self.positions[symbol] = pos
        else:
            pos.legs.append(leg)
            total_size = sum(l.size for l in pos.legs)
            weighted = sum(l.size * l.entry_price for l in pos.legs) / total_size
            pos.size = total_size
            pos.entry_price = weighted
            pos.last_fill_price = entry_px

        self.log_event(
            "leg_open",
            {
                "symbol": symbol,
                "side": side,
                "leg_index": len(pos.legs),
                "leg_size": filled,
                "leg_entry_price": entry_px,
                "avg_entry_price": pos.entry_price,
                "total_size": pos.size,
                "equity": self.equity,
            },
        )
        return pos

    def close_all_legs(
        self,
        symbol: str,
        exit_at: float,
        exit_reason: str,
    ) -> Optional[TradeResult]:
        """Close the entire composite position via a single reduceOnly market order."""
        pos = self.positions.get(symbol)
        if not pos:
            return None

        close_side: Side = "sell" if pos.side == "buy" else "buy"
        try:
            resp = self.exchange.create_market_order(
                symbol, close_side, pos.size, params={"reduceOnly": True}
            )
        except Exception as exc:
            self.log_event(
                "close_error",
                {"symbol": symbol, "exit_reason": exit_reason, "error": str(exc)},
            )
            return None

        fill_price = float(resp.get("average", exit_at) or exit_at)
        total_size = pos.size
        self._sync_equity()

        raw_pnl = (fill_price - pos.entry_price) * total_size
        if pos.side == "sell":
            raw_pnl = -raw_pnl
        fee = self._exit_fee(fill_price * total_size)
        net_pnl = raw_pnl - fee

        trade = TradeResult(
            symbol=symbol,
            side=pos.side,
            size=total_size,
            entry_price=pos.entry_price,
            exit_price=fill_price,
            pnl=net_pnl,
            fees=fee + sum(l.fee for l in pos.legs),
            opened_at=pos.opened_at,
            closed_at=self._now(),
            exit_reason=exit_reason,
            legs=len(pos.legs),
        )
        del self.positions[symbol]

        payload = asdict(trade)
        payload["opened_at"] = trade.opened_at.isoformat()
        payload["closed_at"] = trade.closed_at.isoformat()
        payload["equity"] = self.equity
        self._write_jsonl("trades.jsonl", payload)
        self.log_event(
            "position_close",
            {
                "symbol": trade.symbol,
                "side": trade.side,
                "size": trade.size,
                "legs": trade.legs,
                "avg_entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "pnl": trade.pnl,
                "fees": trade.fees,
                "equity": self.equity,
                "exit_reason": exit_reason,
            },
        )
        return trade

    # ------------------------------------------------------------------
    # Legacy single-leg exit, kept for back-compat
    # ------------------------------------------------------------------

    def close_position_at(
        self,
        symbol: str,
        exit_at: float,
        exit_reason: str,
    ) -> Optional[TradeResult]:
        """Deprecated: forwards to ``close_all_legs``."""
        return self.close_all_legs(symbol, exit_at, exit_reason)
