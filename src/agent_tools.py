"""Single source of truth for **agent-callable tools**.

Both the MCP server (``src/mcp_server.py``) and the two-way Telegram controller
(``src/telegram_control.py``) expose *exactly* the tools defined here — nothing
more.  This is the security boundary for the whole "agentic" layer:

    READ tools      -> always safe, no side effects on trading
    CONTROL tools   -> pause_trading / resume_trading ONLY

There is intentionally **no tool to open, close, or modify a position**, and no
tool to switch the bot to live mode.  Because every agent surface is built on
this registry, the constraint "the agent has no right to open/close positions"
is enforced by construction, not by convention.

Every tool takes a ``Settings`` first argument and returns a JSON-serializable
``dict`` so the same function works over MCP (structured) and Telegram (text).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import control, log_hygiene
from .briefing import _read_jsonl, collect_window  # reuse existing log readers
from .config import Settings

# ---------------------------------------------------------------------------
# Low-level log helpers
# ---------------------------------------------------------------------------

def _logs_dir(settings: Settings) -> Path:
    return Path(settings.logs_dir)


def _tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    rows = list(_read_jsonl(path))
    if limit > 0:
        rows = rows[-limit:]
    return rows


def _latest_event(
    path: Path,
    event_name: str,
    require_key: str | None = None,
) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for row in _read_jsonl(path):
        if row.get("event") != event_name:
            continue
        if require_key is not None and require_key not in row:
            continue
        latest = row
    return latest


def _latest_snapshot(logs: Path) -> dict[str, Any] | None:
    """Newest full bot snapshot.

    Routine heartbeats omit ``positions``/``statuses`` (see ``log_hygiene``), so
    read ``logs/state.json`` first and fall back to the last verbose heartbeat
    for older log sets such as the committed ``demo_logs/``.
    """
    snapshot = log_hygiene.read_state_snapshot(logs)
    if snapshot:
        return snapshot
    return _latest_event(logs / "events.jsonl", "heartbeat", require_key="positions")


def _parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def _demo_reference_now(logs: Path) -> datetime | None:
    """For committed ``demo_logs/``, anchor time windows to the latest heartbeat."""
    if logs.name != "demo_logs":
        return None
    hb = _latest_event(logs / "events.jsonl", "heartbeat")
    if not hb or not hb.get("ts"):
        return None
    return _parse_iso(str(hb["ts"]))


# ---------------------------------------------------------------------------
# READ tools
# ---------------------------------------------------------------------------

def get_status(settings: Settings) -> dict[str, Any]:
    """High-level snapshot: equity, open positions, pause state, last heartbeat."""
    logs = _logs_dir(settings)
    events_path = logs / "events.jsonl"
    hb = _latest_event(events_path, "heartbeat")
    snapshot = _latest_snapshot(logs)
    ctrl = control.read_control(logs)

    status: dict[str, Any] = {
        "mode": settings.mode,
        "symbols": settings.symbols,
        "paused": ctrl["paused"],
        "pause_reason": ctrl["reason"],
        "pause_updated_at": ctrl["updated_at"],
        "equity": None,
        "open_positions": 0,
        "positions": [],
        "last_heartbeat_ts": None,
        "statuses": [],
    }
    scalars = hb or snapshot
    if scalars:
        status["equity"] = scalars.get("equity")
        status["open_positions"] = scalars.get("open_positions", 0)
        status["last_heartbeat_ts"] = scalars.get("ts")
    if snapshot:
        status["positions"] = snapshot.get("positions", [])
        status["statuses"] = snapshot.get("statuses", [])
    return status


def get_pnl(settings: Settings, hours: int = 24) -> dict[str, Any]:
    """Aggregated P&L / risk stats over the trailing ``hours`` window."""
    hours = max(1, int(hours))
    logs = _logs_dir(settings)
    stats = collect_window(logs, hours=hours, now=_demo_reference_now(logs))
    return {
        "window_hours": hours,
        "since_utc": stats.since.isoformat(),
        "until_utc": stats.until.isoformat(),
        "trades": stats.trade_summary,
        "events": stats.event_summary,
    }


def get_recent_trades(settings: Settings, limit: int = 10) -> dict[str, Any]:
    """The most recent closed trades (newest last)."""
    limit = max(1, min(int(limit), 100))
    rows = _tail_jsonl(_logs_dir(settings) / "trades.jsonl", limit)
    keep = (
        "symbol", "side", "size", "entry_price", "exit_price",
        "pnl", "fees", "exit_reason", "legs", "opened_at", "closed_at", "equity",
    )
    trades = [{k: r.get(k) for k in keep} for r in rows]
    return {"count": len(trades), "trades": trades}


def get_open_positions(settings: Settings) -> dict[str, Any]:
    """Structured open positions taken from the latest snapshot."""
    snapshot = _latest_snapshot(_logs_dir(settings))
    positions = snapshot.get("positions", []) if snapshot else []
    return {
        "as_of": snapshot.get("ts") if snapshot else None,
        "count": len(positions),
        "positions": positions,
    }


def get_recent_events(settings: Settings, limit: int = 20) -> dict[str, Any]:
    """Recent non-heartbeat events (opens, adds, exits, errors)."""
    limit = max(1, min(int(limit), 200))
    rows = list(_read_jsonl(_logs_dir(settings) / "events.jsonl"))
    notable = [r for r in rows if r.get("event") != "heartbeat"]
    return {"count": min(limit, len(notable)), "events": notable[-limit:]}


def get_last_briefing(settings: Settings) -> dict[str, Any]:
    """The most recent daily briefing text, if any has been generated."""
    rows = _tail_jsonl(_logs_dir(settings) / "briefings.jsonl", 1)
    if not rows:
        return {"available": False, "briefing": None}
    last = rows[-1]
    return {
        "available": True,
        "ts": last.get("ts"),
        "window_since": last.get("window_since"),
        "window_until": last.get("window_until"),
        "briefing_text": last.get("briefing_text"),
    }


# ---------------------------------------------------------------------------
# CONTROL tools (pause / resume ONLY)
# ---------------------------------------------------------------------------

def pause_trading(settings: Settings, reason: str = "", by: str = "agent") -> dict[str, Any]:
    """Block new entries. Does NOT close or modify open positions."""
    rec = control.set_paused(_logs_dir(settings), True, reason=reason, by=by)
    return {"ok": True, "action": "pause", **rec}


def resume_trading(settings: Settings, by: str = "agent") -> dict[str, Any]:
    """Allow new entries again. Existing positions are unaffected."""
    rec = control.set_paused(_logs_dir(settings), False, reason="", by=by)
    return {"ok": True, "action": "resume", **rec}


# ---------------------------------------------------------------------------
# Tool registry / specs (used by MCP + Telegram NL routing)
# ---------------------------------------------------------------------------

# name -> (callable, {param: type-hint-string}, human description)
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "get_status": {
        "fn": get_status,
        "params": {},
        "description": "Current bot status: mode, equity, open position count, and whether trading is paused.",
    },
    "get_pnl": {
        "fn": get_pnl,
        "params": {"hours": "int (lookback window, default 24)"},
        "description": "Aggregated profit/loss, win/loss counts, fees and risk events over the last N hours.",
    },
    "get_recent_trades": {
        "fn": get_recent_trades,
        "params": {"limit": "int (how many recent trades, default 10)"},
        "description": "List the most recent closed trades with entry/exit prices and P&L.",
    },
    "get_open_positions": {
        "fn": get_open_positions,
        "params": {},
        "description": "List the currently open positions (symbol, legs, average entry, unrealized %).",
    },
    "get_recent_events": {
        "fn": get_recent_events,
        "params": {"limit": "int (how many recent events, default 20)"},
        "description": "Recent notable events: position opens, trailing-stop exits, errors.",
    },
    "get_last_briefing": {
        "fn": get_last_briefing,
        "params": {},
        "description": "The text of the most recent daily Gemini briefing, if one has been generated.",
    },
    "pause_trading": {
        "fn": pause_trading,
        "params": {"reason": "str (optional note explaining why)"},
        "description": "Pause NEW trades. Open positions are NOT closed. Use to stop the bot entering new positions.",
    },
    "resume_trading": {
        "fn": resume_trading,
        "params": {},
        "description": "Resume normal trading after a pause.",
    },
}

# Tools that change state; everything else is read-only. (No open/close exists.)
CONTROL_TOOLS = {"pause_trading", "resume_trading"}


def call_tool(settings: Settings, name: str, args: dict[str, Any] | None = None, by: str = "agent") -> dict[str, Any]:
    """Dispatch a tool by name with kwargs. Raises KeyError for unknown tools."""
    spec = TOOL_SPECS.get(name)
    if spec is None:
        raise KeyError(f"Unknown tool: {name}")
    fn: Callable[..., dict[str, Any]] = spec["fn"]
    kwargs = dict(args or {})
    if name in CONTROL_TOOLS:
        kwargs.setdefault("by", by)
    return fn(settings, **kwargs)


def tools_catalog_text() -> str:
    """Render the tool catalog as plain text for an LLM router prompt."""
    lines = []
    for name, spec in TOOL_SPECS.items():
        params = spec["params"]
        param_str = ", ".join(f"{k}: {v}" for k, v in params.items()) or "(none)"
        lines.append(f"- {name}({param_str}) — {spec['description']}")
    return "\n".join(lines)
