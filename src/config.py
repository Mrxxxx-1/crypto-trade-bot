"""Load bot configuration from environment variables (via ``python-dotenv``).

``Settings`` is a frozen dataclass consumed by the bot, exchange, risk, and
strategy modules.  Defaults are suitable for paper trading on Hyperliquid
perpetual futures (USDC-margined) under the EMA/ATR trend-following strategy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List

from dotenv import load_dotenv

STRATEGIES = ("trend", "hedge")


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


def _parse_strategy(raw: str) -> str:
    """Validate ``STRATEGY``; raise on anything unknown.

    The retired ``dca`` value gets a specific message, since an old ``.env``
    carried over from a previous deploy would otherwise fail cryptically.
    """
    name = (raw or "trend").strip().lower() or "trend"
    if name == "dca":
        raise ValueError(
            "STRATEGY=dca has been removed. Use STRATEGY=trend (directional "
            "trading) or STRATEGY=hedge (manual catalyst hedge only)."
        )
    if name not in STRATEGIES:
        raise ValueError(f"STRATEGY must be one of {STRATEGIES}, got '{raw}'")
    return name


def _parse_direction_mode(raw: str) -> str:
    """Validate ``DIRECTION_MODE``; raise on typos so they surface at startup.

    A silent fallback here would be dangerous: mistyping ``signal`` would leave
    the bot pinned to ``SHORT_SYMBOLS`` while the operator believed direction
    was being read from the trend.
    """
    mode = (raw or "static").strip().lower()
    if mode not in ("static", "signal"):
        raise ValueError(
            f"DIRECTION_MODE must be 'static' or 'signal', got '{raw}'"
        )
    return mode


@dataclass(frozen=True)
class Settings:
    mode: str
    wallet_address: str
    private_key: str
    testnet: bool

    symbols: List[str]
    short_symbols: List[str]           # base coins pinned short under DIRECTION_MODE=static
    strategy: str                      # "trend" or "hedge" (manual catalyst hedge only)
    timeframe: str
    poll_seconds: int
    lookback_candles: int

    initial_equity: float
    max_leverage: float
    max_daily_loss_pct: float
    max_consecutive_losses: int

    # --- Trend-following strategy parameters (STRATEGY=trend) ---
    # EMA cross + regime EMA filter, ATR initial stop, ATR chandelier trail,
    # fixed-fractional (risk %) position sizing.
    trend_ema_period: int              # regime EMA: price must be on its favorable side
    risk_per_trade_pct: float          # % of equity risked per trade (stop distance = risk)
    direction_mode: str                # "static" (SHORT_SYMBOLS decides) or "signal" (trend decides)
    fast_ema: int                      # fast EMA for the trend cross
    slow_ema: int                      # slow EMA for the trend cross
    atr_period: int                    # ATR lookback for stops/sizing
    stop_atr_multiplier: float         # initial stop distance = this * ATR
    trail_atr_multiplier: float        # chandelier trail distance = this * ATR

    # --- Entry-quality filters (opt-in; defaults keep them disabled) ---
    adx_period: int                    # ADX lookback (Wilder)
    adx_min: float                     # min ADX to allow trend entries; 0 disables the chop filter
    volume_ma_period: int              # SMA window for the volume baseline
    volume_min_mult: float             # entry bar volume must exceed this * volume MA; 0 disables
    mtf_enabled: bool                  # require higher-timeframe trend alignment for entries
    mtf_timeframe: str                 # higher timeframe (e.g. "4h") for the MTF filter
    mtf_ema_period: int                # EMA period on the higher timeframe

    consec_halt_hours: float
    daily_loss_halt_hours: float

    entry_fee_bps: float
    exit_fee_bps: float
    slippage_bps: float
    heartbeat_interval: int
    logs_dir: str

    daily_briefing_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    gemini_api_key: str
    daily_briefing_hour_utc: int

    # --- Manual catalyst hedge (see src/hedge.py) ---
    # A hedge holds mirrored long/short legs on one coin. Hyperliquid nets one
    # position per coin, so the short leg is routed to a sub-account via the
    # signed action's vaultAddress field.
    hedge_enabled: bool                # master switch; False disables all hedge code paths
    hedge_sub_account: str             # sub-account address holding the short leg
    hedge_sub_private_key: str         # separate API wallet for the sub leg; see note below
    hedge_symbols: List[str]           # coins the hedge may be armed on
    hedge_risk_pct: float              # % of combined equity risked per leg
    hedge_stop_atr_mult: float         # initial stop distance = this * reference ATR
    hedge_trail_atr_mult: float        # winner's chandelier distance = this * reference ATR
    hedge_atr_floor_pct: float         # ATR percentile floor: guards against sizing off a squeezed ATR
    hedge_max_hours: float             # auto-close an un-triggered hedge after this long; 0 disables
    hedge_expiry_hours: float          # an armed-but-unfilled request expires after this long

    # Web dashboard + agentic control surfaces
    dashboard_host: str
    dashboard_port: int
    dashboard_demo_mode: bool
    telegram_control_enabled: bool

    @property
    def is_live(self) -> bool:
        return self.mode.lower().strip() == "live"

    @property
    def telegram_control_configured(self) -> bool:
        """Two-way control needs a bot token + an authorized chat id.

        Gemini is optional (only used for natural-language routing); slash
        commands work without it.
        """
        return bool(self.telegram_bot_token.strip() and self.telegram_chat_id.strip())

    @property
    def daily_briefing_configured(self) -> bool:
        return bool(
            self.telegram_bot_token.strip()
            and self.telegram_chat_id.strip()
            and self.gemini_api_key.strip()
        )

    @property
    def hedge_configured(self) -> bool:
        """A hedge needs a funded sub-account address to hold the short leg."""
        return bool(self.hedge_enabled and self.hedge_sub_account.strip())

    def direction_for(self, symbol: str) -> str:
        """Return ``"short"`` if the symbol's base is in ``short_symbols``, else ``"long"``.

        Only consulted under ``DIRECTION_MODE=static``, where a symbol is pinned
        to one side for the life of the process. Under ``signal`` the trend
        decides instead -- see ``src/direction.py``.
        """
        base = symbol.split("/")[0].strip().upper() if "/" in symbol else symbol.strip().upper()
        return "short" if base in self.short_symbols else "long"


def load_settings() -> Settings:
    """Load ``.env``, parse every variable with its documented default, return Settings."""
    load_dotenv()

    symbols = [
        s.strip()
        for s in os.getenv("SYMBOLS", "BTC/USDC:USDC,ETH/USDC:USDC").split(",")
        if s.strip()
    ]
    short_symbols = [
        s.strip().upper()
        for s in os.getenv("SHORT_SYMBOLS", "").split(",")
        if s.strip()
    ]

    return Settings(
        mode=os.getenv("MODE", "paper"),
        wallet_address=os.getenv("HL_WALLET_ADDRESS", "").strip(),
        private_key=os.getenv("HL_PRIVATE_KEY", "").strip(),
        testnet=os.getenv("HL_TESTNET", "").lower() in ("1", "true", "yes"),
        symbols=symbols,
        short_symbols=short_symbols,
        strategy=_parse_strategy(os.getenv("STRATEGY", "trend")),
        timeframe=os.getenv("TIMEFRAME", "15m"),
        poll_seconds=_as_int("POLL_SECONDS", 30),
        lookback_candles=_as_int("LOOKBACK_CANDLES", 200),
        initial_equity=_as_float("INITIAL_EQUITY", 10_000),
        max_leverage=_as_float("MAX_LEVERAGE", 3),
        max_daily_loss_pct=_as_float("MAX_DAILY_LOSS_PCT", 2.0),
        max_consecutive_losses=_as_int("MAX_CONSECUTIVE_LOSSES", 3),

        # Trend-following strategy
        trend_ema_period=_as_int("TREND_EMA_PERIOD", 200),
        risk_per_trade_pct=_as_float("RISK_PER_TRADE_PCT", 0.5),
        direction_mode=_parse_direction_mode(os.getenv("DIRECTION_MODE", "static")),
        fast_ema=_as_int("FAST_EMA", 9),
        slow_ema=_as_int("SLOW_EMA", 21),
        atr_period=_as_int("ATR_PERIOD", 14),
        stop_atr_multiplier=_as_float("STOP_ATR_MULTIPLIER", 2.5),
        trail_atr_multiplier=_as_float("TRAIL_ATR_MULTIPLIER", 2.0),

        # Entry-quality filters (opt-in; defaults below leave them disabled)
        adx_period=_as_int("ADX_PERIOD", 14),
        adx_min=_as_float("ADX_MIN", 0.0),
        volume_ma_period=_as_int("VOLUME_MA_PERIOD", 20),
        volume_min_mult=_as_float("VOLUME_MIN_MULT", 0.0),
        mtf_enabled=os.getenv("MTF_ENABLED", "").lower() in ("1", "true", "yes"),
        mtf_timeframe=os.getenv("MTF_TIMEFRAME", "4h").strip() or "4h",
        mtf_ema_period=_as_int("MTF_EMA_PERIOD", 50),

        consec_halt_hours=_as_float("CONSEC_HALT_HOURS", 6),
        daily_loss_halt_hours=_as_float("DAILY_LOSS_HALT_HOURS", 12),
        entry_fee_bps=_as_float("ENTRY_FEE_BPS", 2),
        exit_fee_bps=_as_float("EXIT_FEE_BPS", 3.5),
        slippage_bps=_as_float("SLIPPAGE_BPS", 2),
        heartbeat_interval=max(1, _as_int("HEARTBEAT_INTERVAL", 5)),
        logs_dir=os.getenv("LOGS_DIR", "logs").strip() or "logs",
        daily_briefing_enabled=os.getenv("DAILY_BRIEFING_ENABLED", "").lower()
        in ("1", "true", "yes"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        daily_briefing_hour_utc=max(
            0, min(23, _as_int("DAILY_BRIEFING_HOUR_UTC", 6))
        ),
        # Manual catalyst hedge (off unless explicitly enabled)
        hedge_enabled=os.getenv("HEDGE_ENABLED", "").lower() in ("1", "true", "yes"),
        hedge_sub_account=os.getenv("HEDGE_SUB_ACCOUNT", "").strip(),
        hedge_sub_private_key=os.getenv("HEDGE_SUB_PRIVATE_KEY", "").strip(),
        hedge_symbols=[
            s.strip()
            for s in os.getenv("HEDGE_SYMBOLS", os.getenv("SYMBOLS", "")).split(",")
            if s.strip()
        ],
        hedge_risk_pct=_as_float("HEDGE_RISK_PCT", 0.5),
        hedge_stop_atr_mult=_as_float("HEDGE_STOP_ATR_MULT", 2.0),
        hedge_trail_atr_mult=_as_float("HEDGE_TRAIL_ATR_MULT", 4.0),
        hedge_atr_floor_pct=_as_float("HEDGE_ATR_FLOOR_PCT", 50.0),
        hedge_max_hours=_as_float("HEDGE_MAX_HOURS", 48.0),
        hedge_expiry_hours=_as_float("HEDGE_EXPIRY_HOURS", 12.0),

        dashboard_host=os.getenv("DASHBOARD_HOST", "0.0.0.0").strip() or "0.0.0.0",
        dashboard_port=_as_int("DASHBOARD_PORT", 8000),
        dashboard_demo_mode=os.getenv("DASHBOARD_DEMO_MODE", "").lower()
        in ("1", "true", "yes"),
        telegram_control_enabled=os.getenv("TELEGRAM_CONTROL_ENABLED", "").lower()
        in ("1", "true", "yes"),
    )
