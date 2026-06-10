"""Cross-process trading control flag (pause / resume).

The trading bot, the web dashboard, the MCP server, and the Telegram
controller all run as separate processes that share the same ``logs/``
directory.  They coordinate the "is new trading allowed?" decision through a
single small JSON file, ``logs/control.json``.

Design rules (important safety boundary):

- ``paused`` only blocks **new** entries and DCA adds.  It NEVER closes,
  opens, or modifies a position.  The bot's own trailing-stop exit logic keeps
  running while paused so open positions stay protected.
- Any agent (Telegram / MCP) may toggle this flag, but agents are *only* ever
  given pause/resume — never order placement — so the worst an agent can do is
  stop the bot from opening new trades.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONTROL_FILENAME = "control.json"


def control_path(logs_dir: str | Path) -> Path:
    return Path(logs_dir) / CONTROL_FILENAME


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_control(logs_dir: str | Path) -> dict[str, Any]:
    """Return the current control state, with safe defaults if missing/corrupt."""
    path = control_path(logs_dir)
    default = {"paused": False, "reason": "", "by": "", "updated_at": None}
    if not path.is_file():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default
    return {
        "paused": bool(data.get("paused", False)),
        "reason": str(data.get("reason", "") or ""),
        "by": str(data.get("by", "") or ""),
        "updated_at": data.get("updated_at"),
    }


def is_paused(logs_dir: str | Path) -> bool:
    return read_control(logs_dir)["paused"]


def set_paused(
    logs_dir: str | Path,
    paused: bool,
    reason: str = "",
    by: str = "",
) -> dict[str, Any]:
    """Persist a new pause/resume state and return the written record."""
    path = control_path(logs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "paused": bool(paused),
        "reason": reason,
        "by": by,
        "updated_at": _utc_now_iso(),
    }
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record
