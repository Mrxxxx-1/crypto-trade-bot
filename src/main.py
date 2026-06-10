"""Entry point: load settings and run the Hyperliquid futures bot.

Supports both ``MODE=paper`` (simulated fills) and ``MODE=live``
(real orders on Hyperliquid via the official Python SDK).
"""
from __future__ import annotations

from .bot import FuturesBot
from .briefing import start_briefing_thread
from .config import load_settings
from .telegram_control import start_control_thread


def main() -> None:
    settings = load_settings()

    if settings.is_live:
        if not settings.wallet_address or not settings.private_key:
            raise RuntimeError(
                "Live mode requires HL_WALLET_ADDRESS and HL_PRIVATE_KEY in .env"
            )
        print(
            "*** LIVE MODE — real orders will be placed on Hyperliquid ***\n"
            f"    Wallet : {settings.wallet_address[:8]}…{settings.wallet_address[-4:]}\n"
            f"    Symbols: {settings.symbols}\n"
            f"    Leverage cap: {settings.max_leverage}x\n"
            f"    Risk/trade: {settings.risk_per_trade_pct}%\n"
            f"    Testnet: {settings.testnet}\n"
        )
    else:
        print(f"Paper mode — no real orders. Symbols: {settings.symbols}")

    if settings.daily_briefing_enabled:
        if settings.daily_briefing_configured:
            start_briefing_thread(settings)
            print(
                f"Daily Telegram briefing enabled (Gemini), UTC hour "
                f"{settings.daily_briefing_hour_utc}:00."
            )
        else:
            print(
                "DAILY_BRIEFING_ENABLED is set but TELEGRAM_BOT_TOKEN / "
                "TELEGRAM_CHAT_ID / GEMINI_API_KEY are missing — skipping briefing."
            )

    if settings.telegram_control_enabled:
        if settings.telegram_control_configured:
            start_control_thread(settings)
            print(
                "Two-way Telegram control enabled — message the bot /help. "
                "Agent can read stats and pause/resume only (never open/close)."
            )
        else:
            print(
                "TELEGRAM_CONTROL_ENABLED is set but TELEGRAM_BOT_TOKEN / "
                "TELEGRAM_CHAT_ID are missing — skipping control listener."
            )

    bot = FuturesBot(settings)
    bot.run_forever()


if __name__ == "__main__":
    main()
