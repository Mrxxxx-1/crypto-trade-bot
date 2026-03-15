"""OKX exchange adapter (via ccxt) and paper broker for simulated execution.

``ExchangeAdapter`` wraps ccxt for OHLCV, ticker, and order calls.
``PaperBroker`` simulates limit entries, fill checks, fee/slippage, and
writes trade + event logs to ``logs/``.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import ccxt

from .config import Settings
from .models import PendingOrder, Position, Side, TradeResult


class ExchangeAdapter:
    """Thin ccxt wrapper for OKX: OHLCV, ticker, and order operations."""
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = ccxt.okx(
            {
                "apiKey": settings.api_key,
                "secret": settings.api_secret,
                "password": settings.api_passphrase,
                "enableRateLimit": True,
            }
        )

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> List[List[float]]:
        return self.client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_last_price(self, symbol: str) -> float:
        ticker = self.client.fetch_ticker(symbol)
        return float(ticker["last"])


class PaperBroker:
    """Simulated execution engine: equity tracking, limit fills, fee/slippage, and JSONL logging."""

    def __init__(self, settings: Settings, exchange: ExchangeAdapter) -> None:
        self.settings = settings
        self.exchange = exchange
        self.equity = settings.initial_equity
        self.positions: Dict[str, Position] = {}
        self.pending_orders: Dict[str, PendingOrder] = {}
        self.logs_dir = Path("logs")
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
        self.log_event("limit_order_placed", {
            "symbol": symbol, "side": side, "size": size,
            "limit_price": limit_price, "stop_price": stop_price,
            "take_profit_price": take_profit_price,
        })
        return order

    def check_pending_fill(self, symbol: str) -> Optional[Position]:
        """Check if a pending limit order would fill at current price."""
        order = self.pending_orders.get(symbol)
        if not order:
            return None

        last = self.exchange.fetch_last_price(symbol)
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

        self.log_event("position_open", {
            "symbol": symbol, "side": order.side, "size": order.size,
            "entry_price": entry, "stop_price": order.stop_price,
            "take_profit_price": order.take_profit_price,
            "fee": fee, "equity": self.equity, "order_type": "limit",
        })
        return pos

    def cancel_pending(self, symbol: str) -> bool:
        if symbol in self.pending_orders:
            del self.pending_orders[symbol]
            self.log_event("limit_order_cancelled", {"symbol": symbol})
            return True
        return False

    def is_pending_expired(self, symbol: str) -> bool:
        """True if the pending order has exceeded ``LIMIT_TIMEOUT_SECONDS``."""
        order = self.pending_orders.get(symbol)
        if not order:
            return False
        elapsed = (self._now() - order.placed_at).total_seconds()
        return elapsed >= self.settings.limit_timeout_seconds

    # ------------------------------------------------------------------
    # Close at exact price level (matches backtest exit model)
    # ------------------------------------------------------------------

    def close_position_at(
        self, symbol: str, exit_at: float, exit_reason: str,
    ) -> Optional[TradeResult]:
        """Close position at an exact stop/TP level with slippage applied."""
        pos = self.positions.get(symbol)
        if not pos:
            return None

        exit_side: Side = "sell" if pos.side == "buy" else "buy"
        slip = self.settings.slippage_bps / 10_000
        if exit_side == "buy":
            exit_px = exit_at * (1 + slip)
        else:
            exit_px = exit_at * (1 - slip)

        notional = exit_px * pos.size
        fee = self._exit_fee(notional)

        raw_pnl = (exit_px - pos.entry_price) * pos.size
        if pos.side == "sell":
            raw_pnl = -raw_pnl
        net_pnl = raw_pnl - fee
        self.equity += net_pnl

        trade = TradeResult(
            symbol=symbol,
            side=pos.side,
            size=pos.size,
            entry_price=pos.entry_price,
            exit_price=exit_px,
            pnl=net_pnl,
            fees=fee,
            opened_at=pos.opened_at,
            closed_at=self._now(),
            exit_reason=exit_reason,
        )
        del self.positions[symbol]
        payload = asdict(trade)
        payload["opened_at"] = trade.opened_at.isoformat()
        payload["closed_at"] = trade.closed_at.isoformat()
        payload["equity"] = self.equity
        self._write_jsonl("trades.jsonl", payload)
        self.log_event("position_close", {
            "symbol": trade.symbol, "side": trade.side, "size": trade.size,
            "entry_price": trade.entry_price, "exit_price": trade.exit_price,
            "pnl": trade.pnl, "fees": trade.fees, "equity": self.equity,
            "exit_reason": exit_reason,
        })
        return trade
