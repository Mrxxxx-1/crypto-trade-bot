"""Sub-account plumbing for the catalyst hedge: routing, status, and preflight.

Hyperliquid holds one net position per coin, so the hedge's two legs must live in
two accounts: the long stays in the main account, the short goes to a
sub-account. Sub-accounts have no private key of their own — you sign with the
master (or an approved API wallet) and set the signed action's ``vaultAddress``
to the sub-account address.

Two facts from the Hyperliquid docs shape this module:

1. *Reads* must use the real account address. Querying the agent's address
   returns empty results, which is a silent-wrong-answer trap.
2. *Nonces are tracked per signer.* One API wallet signing for both the master
   and a sub-account shares a single nonce set, and the docs recommend a
   separate API wallet per sub-account. The hedge fires both legs at once, so
   ``HEDGE_SUB_PRIVATE_KEY`` should be a second approved API wallet. It falls
   back to the main key with a warning.

Creating and funding a sub-account are *owner-level* actions that an API wallet
cannot perform, so this module deliberately does not automate them — see
``status`` output and the README for the manual steps.

Usage:
    python -m src.subaccount status
    python -m src.subaccount preflight
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from typing import Any, Optional

from eth_account import Account
from hyperliquid.exchange import Exchange as HLExchange
from hyperliquid.info import Info
from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL

from .config import Settings, load_settings
from .exchange import ExchangeAdapter


def _base_url(settings: Settings) -> str:
    return TESTNET_API_URL if settings.testnet else MAINNET_API_URL


def derive_address(private_key: str) -> Optional[str]:
    """Address a signing key derives to, or None when the key is unusable."""
    key = (private_key or "").strip()
    if not key:
        return None
    try:
        return Account.from_key(key).address
    except (ValueError, TypeError):
        return None


class SubAccountAdapter(ExchangeAdapter):
    """An ``ExchangeAdapter`` whose reads and writes both target a sub-account.

    Reads are redirected by handing the parent a ``Settings`` copy whose
    ``wallet_address`` is the sub-account. Writes are redirected by rebuilding
    the signer with ``vault_address`` set, which is what puts ``vaultAddress``
    into every signed action.
    """

    def __init__(self, settings: Settings, sub_address: str, sub_private_key: str = "") -> None:
        sub_address = sub_address.strip()
        if not sub_address:
            raise ValueError("sub-account address is required")

        signing_key = (sub_private_key or settings.private_key or "").strip()
        sub_settings = replace(
            settings,
            wallet_address=sub_address,
            private_key=signing_key,
        )
        # Let the parent set up Info and size decimals against the sub-account,
        # then replace the signer with a vault-routed one.
        super().__init__(sub_settings)
        self.sub_address = sub_address

        if signing_key:
            wallet = Account.from_key(signing_key)
            self._hlx = HLExchange(
                wallet,
                base_url=_base_url(settings),
                vault_address=sub_address,
                timeout=15.0,
            )
        else:
            self._hlx = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SubAccountAdapter(sub={self.sub_address})"


def build_adapters(settings: Settings) -> tuple[ExchangeAdapter, SubAccountAdapter]:
    """The (main, sub) adapter pair the hedge trades through."""
    if not settings.hedge_sub_account.strip():
        raise ValueError("HEDGE_SUB_ACCOUNT is not set")
    main_adapter = ExchangeAdapter(settings)
    sub_adapter = SubAccountAdapter(
        settings,
        settings.hedge_sub_account,
        settings.hedge_sub_private_key,
    )
    return main_adapter, sub_adapter


# ---------------------------------------------------------------------------
# Status + preflight
# ---------------------------------------------------------------------------

def status(settings: Settings) -> dict[str, Any]:
    """Read-only picture of the master, its agents, and its sub-accounts."""
    adapter = ExchangeAdapter(settings)
    info = Info(base_url=_base_url(settings), skip_ws=True, timeout=15.0)
    main_addr = settings.wallet_address.strip()

    out: dict[str, Any] = {
        "network": "testnet" if settings.testnet else "mainnet",
        "main_address": main_addr,
        "hedge_enabled": settings.hedge_enabled,
        "hedge_sub_account": settings.hedge_sub_account or None,
    }

    signer = derive_address(settings.private_key)
    out["signer_address"] = signer
    if signer and main_addr and signer.lower() == main_addr.lower():
        out["signer_kind"] = "owner"
    elif signer:
        out["signer_kind"] = "agent"
    else:
        out["signer_kind"] = "none"

    sub_signer = derive_address(settings.hedge_sub_private_key)
    out["sub_signer_address"] = sub_signer
    out["sub_signer_shares_nonces_with_main"] = bool(
        signer and (not sub_signer or sub_signer.lower() == signer.lower())
    )

    if not main_addr:
        out["error"] = "HL_WALLET_ADDRESS is not set"
        return out

    try:
        out["main_role"] = info.user_role(main_addr)
    except Exception as exc:  # noqa: BLE001
        out["main_role"] = f"lookup failed: {exc}"

    if signer:
        try:
            out["signer_role"] = info.user_role(signer)
        except Exception as exc:  # noqa: BLE001
            out["signer_role"] = f"lookup failed: {exc}"

    try:
        out["approved_agents"] = info.extra_agents(main_addr) or []
    except Exception as exc:  # noqa: BLE001
        out["approved_agents"] = f"lookup failed: {exc}"

    try:
        subs = info.query_sub_accounts(main_addr)
    except Exception as exc:  # noqa: BLE001
        subs = None
        out["sub_accounts_error"] = str(exc)
    out["sub_accounts"] = subs or []

    try:
        out["main_equity"] = adapter.fetch_balance()
    except Exception as exc:  # noqa: BLE001
        out["main_equity"] = f"lookup failed: {exc}"

    if settings.hedge_sub_account.strip():
        try:
            sub_adapter = SubAccountAdapter(
                settings, settings.hedge_sub_account, settings.hedge_sub_private_key
            )
            out["sub_equity"] = sub_adapter.fetch_balance()
            out["sub_positions"] = sub_adapter.fetch_positions()
        except Exception as exc:  # noqa: BLE001
            out["sub_equity"] = f"lookup failed: {exc}"

    return out


def preflight(settings: Settings) -> dict[str, Any]:
    """Check every precondition the hedge needs, without placing an order.

    Returns ``{"ready": bool, "checks": [...], "blockers": [...]}``. Each check
    carries a human-readable ``fix`` when it fails, because the fixes are mostly
    manual steps in the Hyperliquid UI.
    """
    snapshot = status(settings)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "", fix: str = "", warn: bool = False) -> None:
        checks.append(
            {"name": name, "ok": bool(ok), "warn": warn, "detail": detail, "fix": fix}
        )

    check(
        "hedge_enabled",
        settings.hedge_enabled,
        detail=f"HEDGE_ENABLED={settings.hedge_enabled}",
        fix="Set HEDGE_ENABLED=true in .env once the rest of this list is green.",
    )
    check(
        "signing_key",
        snapshot.get("signer_kind") != "none",
        detail=f"signer={snapshot.get('signer_address')} ({snapshot.get('signer_kind')})",
        fix="Set HL_PRIVATE_KEY to an approved API wallet key.",
    )

    subs = snapshot.get("sub_accounts")
    sub_list = subs if isinstance(subs, list) else []
    check(
        "sub_account_exists",
        bool(sub_list),
        detail=f"{len(sub_list)} sub-account(s) on the master",
        fix=(
            "Create one in the Hyperliquid UI (Portfolio -> Sub-Accounts -> Create). "
            "An API wallet cannot create sub-accounts; this needs your owner wallet."
        ),
    )

    configured_sub = settings.hedge_sub_account.strip().lower()
    known = {str(s.get("subAccountUser", "")).lower() for s in sub_list if isinstance(s, dict)}
    agents = snapshot.get("approved_agents")
    agent_addrs = {
        str(a.get("address", "")).lower()
        for a in (agents if isinstance(agents, list) else [])
        if isinstance(a, dict)
    }
    configured_ok = bool(configured_sub) and (not known or configured_sub in known)
    sub_fix = "Set HEDGE_SUB_ACCOUNT to one of the sub-account addresses listed by `status`."
    if configured_sub and configured_sub in agent_addrs:
        names = [
            str(a.get("name") or a.get("address"))
            for a in agents
            if isinstance(a, dict) and str(a.get("address", "")).lower() == configured_sub
        ]
        label = names[0] if names else configured_sub
        sub_fix = (
            f"HEDGE_SUB_ACCOUNT is set to the API wallet ({label}), not the sub-account. "
            "Put the sub-account address from the UI (Portfolio -> Sub-Accounts) in "
            "HEDGE_SUB_ACCOUNT, and keep the API wallet's private key in HEDGE_SUB_PRIVATE_KEY."
        )
    check(
        "sub_account_configured",
        configured_ok,
        detail=f"HEDGE_SUB_ACCOUNT={settings.hedge_sub_account or '(unset)'}",
        fix=sub_fix,
    )

    sub_equity = snapshot.get("sub_equity")
    has_sub_margin = isinstance(sub_equity, (int, float)) and sub_equity > 0
    check(
        "sub_account_funded",
        has_sub_margin,
        detail=f"sub equity={sub_equity}",
        fix=(
            "Transfer USDC into the sub-account in the Hyperliquid UI. An even split "
            "of your current equity gives each leg the same margin."
        ),
    )

    main_equity = snapshot.get("main_equity")
    if isinstance(main_equity, (int, float)) and has_sub_margin:
        total = main_equity + sub_equity
        share = (min(main_equity, sub_equity) / total * 100) if total > 0 else 0.0
        check(
            "split_is_even",
            share >= 40.0,
            detail=f"main={main_equity:.2f}, sub={sub_equity:.2f} (smaller side {share:.0f}%)",
            fix="Rebalance in the UI so each account holds a similar amount.",
            warn=True,
        )

    check(
        "separate_sub_signer",
        not snapshot.get("sub_signer_shares_nonces_with_main", True),
        detail=(
            "sub leg signs with the same API wallet as the main leg"
            if snapshot.get("sub_signer_shares_nonces_with_main", True)
            else f"sub signer={snapshot.get('sub_signer_address')}"
        ),
        fix=(
            "Approve a second API wallet on the master and set HEDGE_SUB_PRIVATE_KEY. "
            "Hyperliquid tracks nonces per signer, so one key signing both legs "
            "simultaneously can collide."
        ),
        warn=True,
    )

    check(
        "hedge_symbols",
        bool(settings.hedge_symbols),
        detail=f"{settings.hedge_symbols}",
        fix="Set HEDGE_SYMBOLS (defaults to SYMBOLS).",
    )

    blockers = [c["name"] for c in checks if not c["ok"] and not c["warn"]]
    warnings = [c["name"] for c in checks if not c["ok"] and c["warn"]]
    return {
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "snapshot": snapshot,
    }


def _print_preflight(result: dict[str, Any]) -> None:
    print()
    print("=" * 68)
    print("  HEDGE PREFLIGHT")
    print("=" * 68)
    for c in result["checks"]:
        if c["ok"]:
            mark = "PASS"
        elif c["warn"]:
            mark = "WARN"
        else:
            mark = "FAIL"
        print(f"  [{mark}] {c['name']}")
        if c["detail"]:
            print(f"         {c['detail']}")
        if not c["ok"] and c["fix"]:
            print(f"         fix: {c['fix']}")
    print("-" * 68)
    if result["ready"]:
        print("  READY: the hedge can be armed.")
    else:
        print(f"  NOT READY. Blockers: {', '.join(result['blockers'])}")
    if result["warnings"]:
        print(f"  Warnings (non-blocking): {', '.join(result['warnings'])}")
    print("=" * 68)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Hyperliquid sub-account status and hedge preflight")
    parser.add_argument("command", choices=["status", "preflight"])
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a report")
    args = parser.parse_args()

    settings = load_settings()

    if args.command == "status":
        print(json.dumps(status(settings), indent=2, default=str))
        return

    result = preflight(settings)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_preflight(result)
    sys.exit(0 if result["ready"] else 1)


if __name__ == "__main__":
    main()
