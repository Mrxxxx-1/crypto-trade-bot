"""Post-refactor smoke check: config, bot construction, and one live tick.

Verifies the things unit tests can't: that a retired ``STRATEGY=dca`` fails
loudly instead of trading something unintended, that the paper broker and the
Telegram/dashboard formatters agree on the position snapshot's shape, and that
one real polling tick completes against Hyperliquid's public market data.

    python scripts/smoke-check.py

Uses paper mode and a throwaway logs directory, so it never touches real
credentials or the live logs.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import agent_tools, telegram_control  # noqa: E402
from src.bot import FuturesBot  # noqa: E402
from src.config import load_settings  # noqa: E402

PAPER_ENV = {
    "MODE": "paper",
    "SYMBOLS": "BTC/USDC:USDC",
    "TIMEFRAME": "4h",
    "STRATEGY": "trend",
    "DIRECTION_MODE": "signal",
    "LOOKBACK_CANDLES": "250",
    "INITIAL_EQUITY": "1000",
    "HEDGE_ENABLED": "false",
    "TELEGRAM_CONTROL_ENABLED": "false",
    "DAILY_BRIEFING_ENABLED": "false",
}

passed = 0
failed = 0


def check(name: str, fn) -> None:
    global passed, failed
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001
        failed += 1
        print(f"  FAIL  {name}\n          {type(exc).__name__}: {exc}")
    else:
        passed += 1
        print(f"  ok    {name}" + (f"  ({detail})" if detail else ""))


def settings_for(**overrides):
    env = dict(PAPER_ENV)
    env.update(overrides)
    with mock.patch("src.config.load_dotenv", lambda *a, **k: None), \
            mock.patch.dict(os.environ, env, clear=True):
        return load_settings()


def retired_strategy_raises() -> str:
    try:
        settings_for(STRATEGY="dca")
    except ValueError as exc:
        assert "removed" in str(exc), f"unhelpful message: {exc}"
        return "raises with a pointer to the replacement"
    raise AssertionError("STRATEGY=dca was accepted; it should raise")


def bad_direction_mode_raises() -> str:
    try:
        settings_for(DIRECTION_MODE="signl")
    except ValueError:
        return "typo caught at startup"
    raise AssertionError("a mistyped DIRECTION_MODE was accepted")


def valid_strategies_load() -> str:
    modes = [settings_for(STRATEGY=s).strategy for s in ("trend", "hedge")]
    assert modes == ["trend", "hedge"], modes
    return ", ".join(modes)


def dropped_settings_are_gone() -> str:
    settings = settings_for()
    gone = [
        "long_max_prices", "initial_dip_pct", "dca_trigger_pct", "max_dca_legs",
        "leg_notional_pct", "trail_activate_pct", "trail_distance_pct",
        "stop_loss_pct", "take_profit_pct", "trend_filter_enabled",
        "dca_chandelier_enabled", "high_lookback_candles", "dip_memory_bars",
        "require_green_confirmation", "atr_min_pct", "take_profit_r",
        "cooldown_candles", "post_stop_candles", "htf_timeframe",
        "stop_atr_source", "trail_after_r", "limit_timeout_seconds",
    ]
    still_there = [f for f in gone if hasattr(settings, f)]
    assert not still_there, f"still present: {still_there}"
    return f"{len(gone)} retired settings absent"


def stale_env_is_ignored() -> str:
    """A VM whose .env still has DCA keys must start, not crash."""
    settings = settings_for(
        DCA_TRIGGER_PCT="10", MAX_DCA_LEGS="5", LONG_MAX_PRICES="BTC:90000",
        TRAIL_ACTIVATE_PCT="5", STOP_LOSS_PCT="30", LIMIT_TIMEOUT_SECONDS="30",
    )
    return f"unknown keys ignored, strategy={settings.strategy}"


def bot_builds_and_ticks() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        settings = settings_for(LOGS_DIR=tmp)
        bot = FuturesBot(settings)
        status = bot._process_symbol(settings.symbols[0])
        assert status and "insufficient_data" not in status, status
        snapshot = bot._positions_snapshot()
        # The formatters must survive the trimmed snapshot shape.
        telegram_control._fmt_positions({"positions": snapshot})
        telegram_control._fmt_status(
            {"mode": "paper", "equity": bot.broker.equity,
             "open_positions": len(snapshot), "positions": snapshot}
        )
        return status.strip()


def read_tools_are_still_read_only() -> str:
    """The registry must stay an exact set, so a new tool can't sneak in.

    Substring matching is no good here (``get_open_positions`` merely *reads*),
    so this pins the allowlist: anything added shows up as unexpected and has
    to be reviewed deliberately.
    """
    allowed = {
        "get_last_briefing", "get_open_positions", "get_pnl",
        "get_recent_events", "get_recent_trades", "get_status",
        "pause_trading", "resume_trading",
    }
    actual = set(agent_tools.TOOL_SPECS)
    unexpected = sorted(actual - allowed)
    assert not unexpected, f"unreviewed tools exposed to the agent: {unexpected}"
    return f"{len(actual)} tools, all read or pause/resume"


print("Smoke check (paper mode, throwaway logs)\n")
check("retired STRATEGY=dca raises a clear error", retired_strategy_raises)
check("mistyped DIRECTION_MODE raises", bad_direction_mode_raises)
check("both surviving strategies load", valid_strategies_load)
check("DCA settings removed from Settings", dropped_settings_are_gone)
check("a stale .env with DCA keys still starts", stale_env_is_ignored)
check("agent tools remain read + pause only", read_tools_are_still_read_only)
check("bot builds and completes one real tick", bot_builds_and_ticks)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
