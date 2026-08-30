"""Keep ``logs/events.jsonl`` small without losing what readers actually use.

Two sources dominated the log volume of a long-running bot:

- **Heartbeats** (~97% of all event lines) repeated a full ``positions`` +
  ``statuses`` snapshot every few minutes, even when nothing changed.
- **Errors** stored the exchange's raw 502/504 HTML page, hundreds of bytes of
  markup per failed poll, in bursts of 100+ lines during an upstream outage.

This module fixes both without changing the log *layout*:

``HeartbeatThinner``
    Drops the heavy keys from routine heartbeats, but keeps a full one
    periodically and whenever the snapshot is interesting (position count or
    pause state changed, or a status is risk-blocked). Briefing keeps counting
    ``blocked_by_risk`` hits, and the dashboard keeps its ``ts``/``equity``
    points, because those fields are never dropped.

``write_state_snapshot`` / ``read_state_snapshot``
    The always-current full snapshot lives in ``logs/state.json`` (overwritten,
    so constant size) so "what are my open positions right now?" no longer
    depends on the last heartbeat line being verbose.

``error_message_compact``
    Turns an HTML error body into ``502 Bad Gateway`` and caps length.

Env knobs (all optional):
    LOG_SLIM_HEARTBEAT=true          # set false to restore verbose heartbeats
    LOG_HEARTBEAT_VERBOSE_EVERY=20   # keep a full snapshot every N heartbeats
    LOG_ERROR_MAX_CHARS=300          # cap on logged error/message strings
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

STATE_FILENAME = "state.json"

# Heartbeat keys that are large and reconstructible from logs/state.json.
HEAVY_HEARTBEAT_KEYS = ("positions", "statuses")

# Payload keys that may carry a raw exception string from the exchange.
ERROR_TEXT_KEYS = ("message", "error")

# A heartbeat whose statuses contain one of these is always logged in full,
# so briefing's risk counters stay accurate.
NOTABLE_STATUS_MARKERS = ("blocked_by_risk",)

DEFAULT_VERBOSE_EVERY = 20
DEFAULT_ERROR_MAX_CHARS = 300

_HTML_MARKERS = ("<html", "<!doctype html", "<head", "<body")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")
_LEADING_CODE_RE = re.compile(r"^\D{0,4}([1-5]\d{2})\b")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


# ---------------------------------------------------------------------------
# Error messages
# ---------------------------------------------------------------------------

def _strip_markup(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def error_message_compact(message: Any, limit: int | None = None) -> str:
    """Return a short, log-friendly version of an exception string.

    HTML gateway pages collapse to their title/heading (``502 Bad Gateway``);
    everything else is passed through and capped at ``limit`` characters.
    """
    text = message if isinstance(message, str) else str(message)
    cap = DEFAULT_ERROR_MAX_CHARS if limit is None else limit
    if cap <= 0:
        cap = DEFAULT_ERROR_MAX_CHARS

    lowered = text.lower()
    if any(marker in lowered for marker in _HTML_MARKERS):
        code_match = _LEADING_CODE_RE.match(text)
        code = code_match.group(1) if code_match else ""
        candidates = [
            _strip_markup(m.group(1))
            for m in (_TITLE_RE.search(text), _H1_RE.search(text))
            if m
        ]
        candidates = [c for c in candidates if c]
        summary = ""
        if code:
            # Prefer the heading that names the status code ("504 Gateway Timeout").
            summary = next((c for c in candidates if code in c), "")
        if not summary:
            summary = next(iter(candidates), "") or _strip_markup(text)
        if code and code not in summary:
            summary = f"{code} {summary}"
        text = summary

    text = _WS_RE.sub(" ", text).strip()
    if len(text) > cap:
        text = text[: max(1, cap - 3)].rstrip() + "..."
    return text


def compact_event_payload(payload: dict | None, limit: int | None = None) -> dict:
    """Copy ``payload`` with any exception text shortened for logging."""
    if not payload:
        return {}
    cap = _env_int("LOG_ERROR_MAX_CHARS", DEFAULT_ERROR_MAX_CHARS) if limit is None else limit
    out = dict(payload)
    for key in ERROR_TEXT_KEYS:
        value = out.get(key)
        if isinstance(value, str) and value:
            out[key] = error_message_compact(value, cap)
    return out


# ---------------------------------------------------------------------------
# Heartbeats
# ---------------------------------------------------------------------------

class HeartbeatThinner:
    """Decide, per heartbeat, whether to log the full snapshot or a slim line."""

    def __init__(self, verbose_every: int | None = None, enabled: bool | None = None) -> None:
        self.enabled = _env_bool("LOG_SLIM_HEARTBEAT", True) if enabled is None else bool(enabled)
        if verbose_every is None:
            verbose_every = _env_int("LOG_HEARTBEAT_VERBOSE_EVERY", DEFAULT_VERBOSE_EVERY)
        self.verbose_every = max(1, int(verbose_every))
        self._count = 0
        self._last_shape: tuple | None = None

    @staticmethod
    def _shape(payload: dict) -> tuple:
        positions = payload.get("positions") or []
        symbols = tuple(
            sorted(str(p.get("symbol", "")) for p in positions if isinstance(p, dict))
        )
        return (payload.get("open_positions"), bool(payload.get("paused")), symbols)

    @staticmethod
    def _is_notable(payload: dict) -> bool:
        for status in payload.get("statuses") or []:
            if isinstance(status, str) and any(m in status for m in NOTABLE_STATUS_MARKERS):
                return True
        return False

    def thin(self, payload: dict) -> dict:
        """Return the heartbeat payload to write to ``events.jsonl``."""
        if not self.enabled:
            return dict(payload)

        self._count += 1
        shape = self._shape(payload)
        changed = shape != self._last_shape
        self._last_shape = shape
        due = self._count % self.verbose_every == 1 or self.verbose_every == 1

        if due or changed or self._is_notable(payload):
            return dict(payload)
        return {k: v for k, v in payload.items() if k not in HEAVY_HEARTBEAT_KEYS}


# ---------------------------------------------------------------------------
# Current-state snapshot (logs/state.json)
# ---------------------------------------------------------------------------

def state_path(logs_dir: str | Path) -> Path:
    return Path(logs_dir) / STATE_FILENAME


def write_state_snapshot(logs_dir: str | Path, payload: dict) -> Path | None:
    """Overwrite ``logs/state.json`` with the latest full snapshot.

    Never raises: a logging failure must not interrupt the trading loop.
    """
    path = state_path(logs_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
        return path
    except (OSError, TypeError, ValueError):
        return None


def read_state_snapshot(logs_dir: str | Path) -> dict | None:
    """Return the latest full snapshot, or ``None`` if unavailable/corrupt."""
    path = state_path(logs_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
