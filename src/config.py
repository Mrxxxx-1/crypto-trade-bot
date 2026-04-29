"""Load bot configuration from environment variables (via ``python-dotenv``).

``Settings`` is a frozen dataclass consumed by the bot, exchange, risk, and
strategy modules.  Defaults are suitable for paper trading on Hyperliquid
perpetual futures (USDC-margined).
"""

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
    wallet_address: str
    private_key: str
    testnet: bool

    symbols: List[str]
    timeframe: str
    poll_seconds: int
    lookback_candles: int

    initial_equity: float
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
    cooldown_candles: (
        int  # candles to wait after a stop exit before same-direction re-entry
    )
    post_stop_candles: (
        int  # candles to wait after a stop exit before any-direction re-entry
    )

    htf_timeframe: str
    stop_atr_source: (
        str  # "primary" = use signal-TF ATR; "htf" = use higher-TF ATR for wider stops
    )
    trail_after_r: (
        float  # activate trailing stop after this many R in profit (0 = disabled)
    )
    trail_atr_multiplier: float  # trailing distance = this × ATR (from stop_atr_source)
    volume_min_mult: float

    consec_halt_hours: float  # hours to halt after max consecutive losses
    daily_loss_halt_hours: float  # hours to halt after rolling drawdown breach

    entry_fee_bps: float
    exit_fee_bps: float
    slippage_bps: float
    limit_timeout_seconds: int
    heartbeat_interval: int
    logs_dir: str

    # Optional: daily Telegram briefing via Gemini (see ``src.briefing``)
    daily_briefing_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    gemini_api_key: str
    daily_briefing_hour_utc: int  # 0–23, minute 0

    @property
    def is_live(self) -> bool:
        return self.mode.lower().strip() == "live"

    @property
    def daily_briefing_configured(self) -> bool:
        return bool(
            self.telegram_bot_token.strip()
            and self.telegram_chat_id.strip()
            and self.gemini_api_key.strip()
        )


def load_settings() -> Settings:
    """Load ``.env``, parse every variable with its documented default, return Settings."""
    load_dotenv()

    symbols = [
        s.strip()
        for s in os.getenv("SYMBOLS", "BTC/USDC:USDC,ETH/USDC:USDC").split(",")
        if s.strip()
    ]

    return Settings(
        mode=os.getenv("MODE", "paper"),
        wallet_address=os.getenv("HL_WALLET_ADDRESS", "").strip(),
        private_key=os.getenv("HL_PRIVATE_KEY", "").strip(),
        testnet=os.getenv("HL_TESTNET", "").lower() in ("1", "true", "yes"),
        symbols=symbols,
        timeframe=os.getenv("TIMEFRAME", "5m"),
        poll_seconds=_as_int("POLL_SECONDS", 20),
        lookback_candles=_as_int("LOOKBACK_CANDLES", 200),
        initial_equity=_as_float("INITIAL_EQUITY", 10_000),
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
        post_stop_candles=_as_int("POST_STOP_CANDLES", 0),
        htf_timeframe=os.getenv("HTF_TIMEFRAME", "1h"),
        stop_atr_source=os.getenv("STOP_ATR_SOURCE", "primary"),
        trail_after_r=_as_float("TRAIL_AFTER_R", 0.0),
        trail_atr_multiplier=_as_float("TRAIL_ATR_MULTIPLIER", 2.0),
        volume_min_mult=_as_float("VOLUME_MIN_MULT", 0.0),
        consec_halt_hours=_as_float("CONSEC_HALT_HOURS", 6),
        daily_loss_halt_hours=_as_float("DAILY_LOSS_HALT_HOURS", 12),
        entry_fee_bps=_as_float("ENTRY_FEE_BPS", 2),
        exit_fee_bps=_as_float("EXIT_FEE_BPS", 5),
        slippage_bps=_as_float("SLIPPAGE_BPS", 2),
        limit_timeout_seconds=_as_int("LIMIT_TIMEOUT_SECONDS", 30),
        heartbeat_interval=max(1, _as_int("HEARTBEAT_INTERVAL", 1)),
        logs_dir=os.getenv("LOGS_DIR", "logs").strip() or "logs",
        daily_briefing_enabled=os.getenv("DAILY_BRIEFING_ENABLED", "").lower()
        in ("1", "true", "yes"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        daily_briefing_hour_utc=max(
            0, min(23, _as_int("DAILY_BRIEFING_HOUR_UTC", 8))
        ),
    )
