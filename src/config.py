"""Load bot configuration from environment variables (via ``python-dotenv``).

``Settings`` is a frozen dataclass consumed by the bot, exchange, risk, and
strategy modules.  Defaults are suitable for paper trading on Hyperliquid
perpetual futures (USDC-margined) under the long-only DCA-on-dips strategy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List

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


def _parse_price_map(raw: str) -> Dict[str, float]:
    """Parse ``LONG_MAX_PRICES=BTC:90000,ETH:3000`` into ``{"BTC": 90000, "ETH": 3000}``.

    Keys match the base of a perp pair like ``BTC/USDC:USDC`` (the part before ``/``).
    Empty / missing entries are skipped silently; malformed entries raise ValueError so
    the user notices typos immediately at startup.
    """
    if not raw:
        return {}
    out: Dict[str, float] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(
                f"LONG_MAX_PRICES entry '{chunk}' missing ':' (expected BASE:PRICE, e.g. BTC:90000)"
            )
        base, price = chunk.split(":", 1)
        base = base.strip().upper()
        if not base:
            continue
        out[base] = float(price.strip())
    return out


@dataclass(frozen=True)
class Settings:
    mode: str
    wallet_address: str
    private_key: str
    testnet: bool

    symbols: List[str]
    short_symbols: List[str]           # base coins traded short-only (e.g. ["TRUMP", "HYPE"])
    strategy: str                      # "dca" (dip-buying) or "trend" (EMA/ATR trend-following)
    timeframe: str
    poll_seconds: int
    lookback_candles: int

    initial_equity: float
    max_leverage: float
    max_daily_loss_pct: float
    max_consecutive_losses: int

    # --- DCA-on-dips strategy parameters (active) ---
    long_max_prices: Dict[str, float]  # per-symbol price ceiling for entries
    initial_dip_pct: float             # % drop from recent high to trigger first leg
    high_lookback_candles: int         # window for "recent high" (e.g. 96 = 24h at 15m)
    dip_memory_bars: int               # how many recent closed bars to scan for a qualifying dip
    require_green_confirmation: bool   # if True, only enter when latest closed bar is green (close > open)
    dca_trigger_pct: float             # % drop from last fill price to add a leg
    leg_notional_pct: float            # % of starting equity per leg (notional)
    max_dca_legs: int                  # hard cap on legs per symbol
    trail_activate_pct: float          # arm trailing once price >= avg_cost * (1 + this/100)
    trail_distance_pct: float          # once armed, exit if price <= peak * (1 - this/100)
    stop_loss_pct: float               # hard stop: exit all legs if price <= avg_cost * (1 - this/100); 0 disables
    take_profit_pct: float             # fixed TP: exit all legs if price >= avg_cost * (1 + this/100); 0 disables
    trend_filter_enabled: bool         # only enter/add when price is above the long EMA (skip downtrends)
    trend_ema_period: int              # EMA period for the trend filter

    # --- Trend-following strategy parameters (STRATEGY=trend) ---
    # EMA cross + 200-EMA regime filter, ATR initial stop, ATR chandelier trail,
    # fixed-fractional (risk %) position sizing. trend_ema_period (above) is the
    # regime EMA shared with the DCA trend filter.
    risk_per_trade_pct: float          # % of equity risked per trade (stop distance = risk)
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
    dca_chandelier_enabled: bool       # use an ATR chandelier trail for the DCA exit instead of % trail

    # --- Deprecated knobs; retained so old .env files don't crash ---
    atr_min_pct: float
    take_profit_r: float
    cooldown_candles: int
    post_stop_candles: int
    htf_timeframe: str
    stop_atr_source: str
    trail_after_r: float

    consec_halt_hours: float
    daily_loss_halt_hours: float

    entry_fee_bps: float
    exit_fee_bps: float
    slippage_bps: float
    limit_timeout_seconds: int
    heartbeat_interval: int
    logs_dir: str

    daily_briefing_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    gemini_api_key: str
    daily_briefing_hour_utc: int

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

    def max_price_for(self, symbol: str) -> float:
        """Return the long-entry price ceiling for ``symbol``; +inf if uncapped."""
        base = symbol.split("/")[0].strip().upper() if "/" in symbol else symbol.strip().upper()
        return self.long_max_prices.get(base, float("inf"))

    def direction_for(self, symbol: str) -> str:
        """Return ``"short"`` if the symbol's base is in ``short_symbols``, else ``"long"``.

        The strategy is long-only by default; only symbols explicitly listed in
        ``SHORT_SYMBOLS`` are traded short (mirror logic).
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
        strategy=os.getenv("STRATEGY", "dca").strip().lower() or "dca",
        timeframe=os.getenv("TIMEFRAME", "15m"),
        poll_seconds=_as_int("POLL_SECONDS", 30),
        lookback_candles=_as_int("LOOKBACK_CANDLES", 200),
        initial_equity=_as_float("INITIAL_EQUITY", 10_000),
        max_leverage=_as_float("MAX_LEVERAGE", 3),
        max_daily_loss_pct=_as_float("MAX_DAILY_LOSS_PCT", 2.0),
        max_consecutive_losses=_as_int("MAX_CONSECUTIVE_LOSSES", 3),

        # DCA strategy (active)
        long_max_prices=_parse_price_map(os.getenv("LONG_MAX_PRICES", "BTC:90000,ETH:3000")),
        initial_dip_pct=_as_float("INITIAL_DIP_PCT", 3.0),
        high_lookback_candles=_as_int("HIGH_LOOKBACK_CANDLES", 96),
        dip_memory_bars=_as_int("DIP_MEMORY_BARS", 6),
        require_green_confirmation=os.getenv("REQUIRE_GREEN_CONFIRMATION", "true").lower()
        in ("1", "true", "yes"),
        dca_trigger_pct=_as_float("DCA_TRIGGER_PCT", 10.0),
        leg_notional_pct=_as_float("LEG_NOTIONAL_PCT", 10.0),
        max_dca_legs=_as_int("MAX_DCA_LEGS", 5),
        trail_activate_pct=_as_float("TRAIL_ACTIVATE_PCT", 5.0),
        trail_distance_pct=_as_float("TRAIL_DISTANCE_PCT", 3.0),
        stop_loss_pct=_as_float("STOP_LOSS_PCT", 0.0),
        take_profit_pct=_as_float("TAKE_PROFIT_PCT", 0.0),
        trend_filter_enabled=os.getenv("TREND_FILTER_ENABLED", "true").lower()
        in ("1", "true", "yes"),
        trend_ema_period=_as_int("TREND_EMA_PERIOD", 200),

        # Deprecated knobs (kept for back-compat with old .env files)
        risk_per_trade_pct=_as_float("RISK_PER_TRADE_PCT", 0.5),
        fast_ema=_as_int("FAST_EMA", 9),
        slow_ema=_as_int("SLOW_EMA", 21),
        atr_period=_as_int("ATR_PERIOD", 14),
        atr_min_pct=_as_float("ATR_MIN_PCT", 0.15),
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
        dca_chandelier_enabled=os.getenv("DCA_CHANDELIER_ENABLED", "").lower()
        in ("1", "true", "yes"),

        # Deprecated knobs (kept for back-compat with old .env files)
        take_profit_r=_as_float("TAKE_PROFIT_R", 2.0),
        cooldown_candles=_as_int("COOLDOWN_CANDLES", 0),
        post_stop_candles=_as_int("POST_STOP_CANDLES", 1),
        htf_timeframe=os.getenv("HTF_TIMEFRAME", "1h"),
        stop_atr_source=os.getenv("STOP_ATR_SOURCE", "htf"),
        trail_after_r=_as_float("TRAIL_AFTER_R", 0.0),

        consec_halt_hours=_as_float("CONSEC_HALT_HOURS", 6),
        daily_loss_halt_hours=_as_float("DAILY_LOSS_HALT_HOURS", 12),
        entry_fee_bps=_as_float("ENTRY_FEE_BPS", 2),
        exit_fee_bps=_as_float("EXIT_FEE_BPS", 3.5),
        slippage_bps=_as_float("SLIPPAGE_BPS", 2),
        limit_timeout_seconds=_as_int("LIMIT_TIMEOUT_SECONDS", 30),
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
        dashboard_host=os.getenv("DASHBOARD_HOST", "0.0.0.0").strip() or "0.0.0.0",
        dashboard_port=_as_int("DASHBOARD_PORT", 8000),
        dashboard_demo_mode=os.getenv("DASHBOARD_DEMO_MODE", "").lower()
        in ("1", "true", "yes"),
        telegram_control_enabled=os.getenv("TELEGRAM_CONTROL_ENABLED", "").lower()
        in ("1", "true", "yes"),
    )
