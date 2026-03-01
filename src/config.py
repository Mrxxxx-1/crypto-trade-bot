from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List

from dotenv import load_dotenv


def _as_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def _as_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    mode: str
    api_key: str
    api_secret: str
    api_passphrase: str

    symbols: List[str]
    timeframe: str
    poll_seconds: int
    lookback_candles: int

    initial_equity: float
    target_leverage: float
    max_leverage: float
    risk_per_trade_pct: float
    max_daily_loss_pct: float
    max_consecutive_losses: int

    fast_ema: int
    slow_ema: int
    atr_period: int
    atr_min_pct: float
    stop_atr_multiplier: float
    take_profit_r: float
    cooldown_candles: int

    htf_timeframe: str
    volume_min_mult: float

    entry_fee_bps: float
    exit_fee_bps: float
    slippage_bps: float
    limit_timeout_seconds: int
    heartbeat_interval: int

    @property
    def is_live(self) -> bool:
        return self.mode.lower().strip() == "live"


def load_settings() -> Settings:
    load_dotenv()

    symbols = [s.strip() for s in os.getenv("SYMBOLS", "BTC-USDT-SWAP,ETH-USDT-SWAP").split(",") if s.strip()]

    return Settings(
        mode=os.getenv("MODE", "paper"),
        api_key=os.getenv("OKX_API_KEY", ""),
        api_secret=os.getenv("OKX_API_SECRET", ""),
        api_passphrase=os.getenv("OKX_API_PASSPHRASE", ""),
        symbols=symbols,
        timeframe=os.getenv("TIMEFRAME", "5m"),
        poll_seconds=_as_int("POLL_SECONDS", 20),
        lookback_candles=_as_int("LOOKBACK_CANDLES", 200),
        initial_equity=_as_float("INITIAL_EQUITY", 10_000),
        target_leverage=_as_float("TARGET_LEVERAGE", 3),
        max_leverage=_as_float("MAX_LEVERAGE", 3),
        risk_per_trade_pct=_as_float("RISK_PER_TRADE_PCT", 0.5),
        max_daily_loss_pct=_as_float("MAX_DAILY_LOSS_PCT", 2.0),
        max_consecutive_losses=_as_int("MAX_CONSECUTIVE_LOSSES", 3),
        fast_ema=_as_int("FAST_EMA", 20),
        slow_ema=_as_int("SLOW_EMA", 50),
        atr_period=_as_int("ATR_PERIOD", 14),
        atr_min_pct=_as_float("ATR_MIN_PCT", 0.2),
        stop_atr_multiplier=_as_float("STOP_ATR_MULTIPLIER", 1.8),
        take_profit_r=_as_float("TAKE_PROFIT_R", 1.5),
        cooldown_candles=_as_int("COOLDOWN_CANDLES", 0),
        htf_timeframe=os.getenv("HTF_TIMEFRAME", "1h"),
        volume_min_mult=_as_float("VOLUME_MIN_MULT", 0.0),
        entry_fee_bps=_as_float("ENTRY_FEE_BPS", 2),
        exit_fee_bps=_as_float("EXIT_FEE_BPS", 5),
        slippage_bps=_as_float("SLIPPAGE_BPS", 2),
        limit_timeout_seconds=_as_int("LIMIT_TIMEOUT_SECONDS", 30),
        heartbeat_interval=max(1, _as_int("HEARTBEAT_INTERVAL", 1)),
    )
