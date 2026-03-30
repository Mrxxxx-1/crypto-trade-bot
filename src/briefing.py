"""Daily performance & risk briefing: aggregate JSONL logs, summarize with Gemini, send via Telegram.

Reads ``logs/trades.jsonl`` and ``logs/events.jsonl`` for the last 24 hours (UTC),
calls the Gemini API for a structured narrative, then posts to Telegram.

Also runnable standalone for cron: ``python -m src.briefing``
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import Settings, load_settings


def _parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


@dataclass
class WindowStats:
    since: datetime
    until: datetime
    trades: list[dict[str, Any]]
    events: list[dict[str, Any]]
    trade_summary: dict[str, Any]
    event_summary: dict[str, Any]


def _summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {
            "count": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "total_fees": 0.0,
            "by_symbol": {},
            "last_equity": None,
        }
    wins = sum(1 for t in trades if float(t.get("pnl", 0)) > 0)
    losses = sum(1 for t in trades if float(t.get("pnl", 0)) < 0)
    total_pnl = sum(float(t.get("pnl", 0)) for t in trades)
    total_fees = sum(float(t.get("fees", 0)) for t in trades)
    by_symbol: dict[str, dict[str, float]] = {}
    for t in trades:
        sym = str(t.get("symbol", "?"))
        if sym not in by_symbol:
            by_symbol[sym] = {"count": 0, "pnl": 0.0}
        by_symbol[sym]["count"] += 1
        by_symbol[sym]["pnl"] += float(t.get("pnl", 0))
    last_eq = trades[-1].get("equity")
    return {
        "count": len(trades),
        "wins": wins,
        "losses": losses,
        "breakeven": len(trades) - wins - losses,
        "total_pnl": round(total_pnl, 4),
        "total_fees": round(total_fees, 4),
        "by_symbol": by_symbol,
        "last_equity": last_eq,
    }


def _summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [e for e in events if e.get("event") == "error"]
    heartbeats = [e for e in events if e.get("event") == "heartbeat"]
    blocked = 0
    last_equity: float | None = None
    for hb in heartbeats:
        eq = hb.get("equity")
        if eq is not None:
            try:
                last_equity = float(eq)
            except (TypeError, ValueError):
                pass
        for st in hb.get("statuses") or []:
            if isinstance(st, str) and "blocked_by_risk" in st:
                blocked += 1
    return {
        "error_count": len(errors),
        "heartbeat_samples": len(heartbeats),
        "blocked_by_risk_status_hits": blocked,
        "last_equity_from_heartbeat": last_equity,
        "error_samples": [e.get("message", "")[:500] for e in errors[-5:]],
    }


def collect_window(
    logs_dir: Path,
    hours: int = 24,
    now: datetime | None = None,
) -> WindowStats:
    """Collect trades and events with timestamps in [since, until)."""
    until = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    since = until - timedelta(hours=hours)

    trades: list[dict[str, Any]] = []
    for row in _read_jsonl(logs_dir / "trades.jsonl"):
        closed = row.get("closed_at")
        if not closed:
            continue
        try:
            ct = _parse_iso(str(closed))
        except (ValueError, TypeError):
            continue
        if since <= ct < until:
            trades.append(row)

    events: list[dict[str, Any]] = []
    for row in _read_jsonl(logs_dir / "events.jsonl"):
        ts = row.get("ts")
        if not ts:
            continue
        try:
            et = _parse_iso(str(ts))
        except (ValueError, TypeError):
            continue
        if since <= et < until:
            events.append(row)

    trades.sort(key=lambda r: str(r.get("closed_at", "")))
    return WindowStats(
        since=since,
        until=until,
        trades=trades,
        events=events,
        trade_summary=_summarize_trades(trades),
        event_summary=_summarize_events(events),
    )


def _gemini_generate(api_key: str, prompt: str) -> str:
    import google.generativeai as genai  # noqa: PLC0415 — optional heavy import

    model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)
    resp = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            temperature=0.3,
            max_output_tokens=2048,
        ),
    )
    text = getattr(resp, "text", None) or ""
    if not text.strip() and getattr(resp, "candidates", None):
        parts = []
        for c in resp.candidates:
            for p in getattr(c.content, "parts", []) or []:
                if getattr(p, "text", None):
                    parts.append(p.text)
        text = "\n".join(parts)
    return text.strip() or "(No text returned from Gemini.)"


def _telegram_send(token: str, chat_id: str, text: str) -> None:
    base = f"https://api.telegram.org/bot{token}/sendMessage"
    max_len = 4000
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")

    for i, chunk in enumerate(chunks):
        prefix = f"[{i + 1}/{len(chunks)}]\n" if len(chunks) > 1 else ""
        body = prefix + chunk
        data = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": body}
        ).encode("utf-8")
        req = urllib.request.Request(base, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read()
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")[:800]
            hint = ""
            if "chat not found" in err_body.lower():
                hint = (
                    "\n\nFix: In Telegram open your bot, send /start once, then set "
                    "TELEGRAM_CHAT_ID to your numeric id (visit "
                    "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates right after messaging the bot, "
                    "or use @userinfobot). For a group, add the bot to the group and use the negative chat id from getUpdates."
                )
            raise RuntimeError(f"Telegram HTTP {exc.code}: {err_body}{hint}") from exc


def _truncate_trade_lines(trades: list[dict[str, Any]], max_lines: int = 40) -> str:
    lines = []
    for t in trades[-max_lines:]:
        lines.append(
            json.dumps(
                {
                    k: t.get(k)
                    for k in (
                        "symbol",
                        "side",
                        "size",
                        "entry_price",
                        "exit_price",
                        "pnl",
                        "fees",
                        "exit_reason",
                        "opened_at",
                        "closed_at",
                    )
                },
                default=str,
            )
        )
    return "\n".join(lines)


def build_prompt(stats: WindowStats) -> str:
    payload = {
        "period_utc": {
            "since": stats.since.isoformat(),
            "until": stats.until.isoformat(),
        },
        "trade_aggregates": stats.trade_summary,
        "event_aggregates": stats.event_summary,
        "trade_log_jsonl": _truncate_trade_lines(stats.trades),
    }
    raw = json.dumps(payload, indent=2, default=str)
    return (
        "You are a disciplined trading operations assistant. Using ONLY the JSON data below "
        "(paper futures bot on OKX), write a concise daily briefing in plain text (no markdown tables).\n\n"
        "Include these sections with short headers:\n"
        "1) Performance — P&L, win/loss count, fees, symbol breakdown if any trades.\n"
        "2) Risk — drawdown context from equity if present, blocked_by_risk frequency, errors.\n"
        "3) Trade log — brief narrative of notable fills/exits (reference exit_reason).\n"
        "4) Watchlist — 2–4 bullet observations or cautions for the next session.\n\n"
        "If there were zero closed trades, say so clearly and focus on risk/events/heartbeat.\n\n"
        "DATA:\n"
        f"{raw}"
    )


def run_daily_briefing(settings: Settings, logs_dir: Path | None = None) -> None:
    """Collect logs, call Gemini, send Telegram. Raises on missing config or API failure."""
    if not settings.daily_briefing_configured:
        raise RuntimeError(
            "Briefing requires TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and GEMINI_API_KEY."
        )
    root = logs_dir or Path("logs")
    stats = collect_window(root, hours=24)
    prompt = build_prompt(stats)
    title = (
        f"Daily bot briefing (UTC)\n"
        f"{stats.since.strftime('%Y-%m-%d %H:%M')} → "
        f"{stats.until.strftime('%Y-%m-%d %H:%M')}\n"
        f"---\n"
    )
    body = _gemini_generate(settings.gemini_api_key, prompt)
    _telegram_send(
        settings.telegram_bot_token,
        settings.telegram_chat_id,
        title + body,
    )


_STATE_NAME = "briefing_state.json"


def _load_last_sent(logs_dir: Path) -> str | None:
    path = logs_dir / _STATE_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("last_sent_utc_date", "")) or None
    except (json.JSONDecodeError, OSError):
        return None


def _save_last_sent(logs_dir: Path, utc_date: str) -> None:
    path = logs_dir / _STATE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"last_sent_utc_date": utc_date}, indent=2),
        encoding="utf-8",
    )


def briefing_scheduler_loop(settings: Settings) -> None:
    """Background loop: once per UTC calendar day at ``DAILY_BRIEFING_HOUR_UTC``."""
    logs_dir = Path("logs")
    while True:
        try:
            now = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")
            last = _load_last_sent(logs_dir)
            if (
                now.hour == settings.daily_briefing_hour_utc
                and last != today
            ):
                run_daily_briefing(settings, logs_dir)
                _save_last_sent(logs_dir, today)
        except Exception as exc:  # noqa: BLE001 — keep thread alive
            print(f"[briefing] ERROR {exc}")
        time.sleep(30)


def start_briefing_thread(settings: Settings) -> threading.Thread | None:
    if not settings.daily_briefing_enabled or not settings.daily_briefing_configured:
        return None
    t = threading.Thread(
        target=briefing_scheduler_loop,
        args=(settings,),
        name="daily-briefing",
        daemon=True,
    )
    t.start()
    return t


def main() -> None:
    """CLI / cron: send one briefing and exit."""
    settings = load_settings()
    if not settings.daily_briefing_configured:
        raise SystemExit(
            "Set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and GEMINI_API_KEY in .env"
        )
    run_daily_briefing(settings)
    print("Briefing sent.")


if __name__ == "__main__":
    main()
