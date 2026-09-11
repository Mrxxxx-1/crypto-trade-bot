"""Apply Hyperliquid leverage + margin mode to an account.

Hyperliquid's ``updateLeverage`` takes ``isCross``. Isolated margin keeps a
losing position from draining the rest of that account's equity. The hedge
already splits legs across two accounts; isolated then caps each leg to its
own reserved margin instead of the whole wallet.
"""

from __future__ import annotations

from typing import Protocol

from .config import Settings


class LeveragedAccount(Protocol):
    def set_leverage(self, leverage: int, symbol: str) -> None: ...


def apply_account_leverage(
    adapter: LeveragedAccount,
    settings: Settings,
    symbols: list[str],
) -> None:
    """Set ``MAX_LEVERAGE`` and ``MARGIN_MODE`` on each symbol of ``adapter``."""
    lev = max(1, int(settings.max_leverage))
    for symbol in symbols:
        adapter.set_leverage(lev, symbol)
