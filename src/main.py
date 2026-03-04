"""Entry point: load settings and run the paper futures bot.

Live mode is intentionally disabled in this MVP -- set MODE=paper.
"""
from __future__ import annotations

from .bot import FuturesBot
from .config import load_settings


def main() -> None:
    settings = load_settings()
    if settings.is_live:
        raise RuntimeError(
            "Live mode is intentionally disabled in this MVP. "
            "Set MODE=paper for fake-money testing first."
        )
    bot = FuturesBot(settings)
    bot.run_forever()


if __name__ == "__main__":
    main()
