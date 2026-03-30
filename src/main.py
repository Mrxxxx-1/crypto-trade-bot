"""Entry point: load settings and run the paper futures bot.

Live mode is intentionally disabled in this MVP -- set MODE=paper.
"""
from __future__ import annotations

from .bot import FuturesBot
from .briefing import start_briefing_thread
from .config import load_settings


def main() -> None:
    settings = load_settings()
    if settings.is_live:
        raise RuntimeError(
            "Live mode is intentionally disabled in this MVP. "
            "Set MODE=paper for fake-money testing first."
        )
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
    bot = FuturesBot(settings)
    bot.run_forever()


if __name__ == "__main__":
    main()
