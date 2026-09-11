"""Tests for ``MARGIN_MODE`` and applying leverage to both hedge accounts."""

from __future__ import annotations

import unittest

from src.margin import apply_account_leverage
from tests.helpers import make_settings


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def set_leverage(self, leverage: int, symbol: str) -> None:
        self.calls.append((leverage, symbol))


class TestMarginMode(unittest.TestCase):
    def test_defaults_to_isolated(self):
        settings = make_settings()
        self.assertEqual(settings.margin_mode, "isolated")
        self.assertFalse(settings.is_cross_margin)

    def test_cross_is_opt_in(self):
        settings = make_settings(MARGIN_MODE="cross")
        self.assertEqual(settings.margin_mode, "cross")
        self.assertTrue(settings.is_cross_margin)

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            make_settings(MARGIN_MODE="isolated-cross")


class TestApplyLeverage(unittest.TestCase):
    def test_sets_max_leverage_on_each_symbol(self):
        settings = make_settings(MAX_LEVERAGE=3)
        adapter = FakeAdapter()
        apply_account_leverage(adapter, settings, ["BTC/USDC:USDC", "ETH/USDC:USDC"])
        self.assertEqual(
            adapter.calls,
            [(3, "BTC/USDC:USDC"), (3, "ETH/USDC:USDC")],
        )

    def test_leverage_floor_is_one(self):
        settings = make_settings(MAX_LEVERAGE=0)
        adapter = FakeAdapter()
        apply_account_leverage(adapter, settings, ["BTC/USDC:USDC"])
        self.assertEqual(adapter.calls, [(1, "BTC/USDC:USDC")])


if __name__ == "__main__":
    unittest.main()
