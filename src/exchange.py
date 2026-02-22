from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import ccxt

from .config import Settings
from .models import Position, Side, TradeResult


class ExchangeAdapter:
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

    def create_market_order(self, symbol: str, side: Side, amount: float) -> dict:
        return self.client.create_order(symbol=symbol, type="market", side=side, amount=amount)


class PaperBroker:
    def __init__(self, settings: Settings, exchange: ExchangeAdapter) -> None:
        self.settings = settings
        self.exchange = exchange
        self.equity = settings.initial_equity
        self.positions: Dict[str, Position] = {}
        self.logs_dir = Path("logs")
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _write_jsonl(self, name: str, payload: dict) -> None:
        path = self.logs_dir / name
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")

    def _execution_price(self, last_price: float, side: Side) -> float:
        slip = self.settings.slippage_bps / 10_000
        if side == "buy":
            return last_price * (1 + slip)
        return last_price * (1 - slip)

    def _fees(self, notional: float) -> float:
        return notional * (self.settings.fee_bps / 10_000)

    def open_position(
        self,
        symbol: str,
        side: Side,
        size: float,
        stop_price: float,
        take_profit_price: float,
    ) -> Optional[Position]:
        if size <= 0 or symbol in self.positions:
            return None
        last = self.exchange.fetch_last_price(symbol)
        entry = self._execution_price(last, side)
        notional = entry * size
        fee = self._fees(notional)
        self.equity -= fee

        pos = Position(
            symbol=symbol,
            side=side,
            size=size,
            entry_price=entry,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            opened_at=self._now(),
        )
        self.positions[symbol] = pos
        self._write_jsonl(
            "events.jsonl",
            {
                "ts": self._now().isoformat(),
                "event": "position_open",
                "symbol": symbol,
                "side": side,
                "size": size,
                "entry_price": entry,
                "stop_price": stop_price,
                "take_profit_price": take_profit_price,
                "fee": fee,
                "equity": self.equity,
            },
        )
        return pos

    def close_position(self, symbol: str) -> Optional[TradeResult]:
        pos = self.positions.get(symbol)
        if not pos:
            return None
        exit_side: Side = "sell" if pos.side == "buy" else "buy"
        last = self.exchange.fetch_last_price(symbol)
        exit_px = self._execution_price(last, exit_side)
        notional = exit_px * pos.size
        fee = self._fees(notional)

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
        )
        del self.positions[symbol]
        payload = asdict(trade)
        payload["opened_at"] = trade.opened_at.isoformat()
        payload["closed_at"] = trade.closed_at.isoformat()
        payload["equity"] = self.equity
        self._write_jsonl("trades.jsonl", payload)
        return trade

    def maybe_force_exit_by_risk(self, symbol: str) -> Optional[TradeResult]:
        pos = self.positions.get(symbol)
        if not pos:
            return None
        last = self.exchange.fetch_last_price(symbol)
        if pos.side == "buy":
            if last <= pos.stop_price or last >= pos.take_profit_price:
                return self.close_position(symbol)
        else:
            if last >= pos.stop_price or last <= pos.take_profit_price:
                return self.close_position(symbol)
        return None
