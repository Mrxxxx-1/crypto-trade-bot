"""Two-way Telegram control: send the bot commands, get answers back.

This turns the existing one-way daily briefing into an interactive, *agentic*
control channel.  You message the bot in Telegram; it either:

  * runs a **slash command** directly (``/status``, ``/pnl 12``, ``/pause`` ...), or
  * for free-text ("how are we doing today?", "stop buying for now"), asks
    **Gemini** to pick the right tool from the catalog and runs it.

Every callable is taken from ``src/agent_tools.py``, whose registry contains
only READ tools plus pause/resume.  There is no open/close tool anywhere, so
the agent can never place an order -- the worst it can do is pause the bot.

Security:
  * Only messages whose chat id matches ``TELEGRAM_CHAT_ID`` are honored;
    everything else is ignored.
  * Uses Telegram long-polling (getUpdates) -- no inbound port, no webhook.

Run standalone:
    python -m src.telegram_control

Or set ``TELEGRAM_CONTROL_ENABLED=true`` to start it inside ``python -m src.main``.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import agent_tools
from .briefing import _gemini_generate, _telegram_send
from .config import Settings, load_settings

_API = "https://api.telegram.org/bot{token}/{method}"

HELP_TEXT = (
    "Crypto bot control. I can read stats and pause/resume trading.\n"
    "I can NOT open or close positions.\n\n"
    "Commands:\n"
    "/status — equity, open positions, pause state\n"
    "/pnl [hours] — P&L summary (default 24h)\n"
    "/trades [n] — recent closed trades (default 10)\n"
    "/positions — current open positions\n"
    "/events [n] — recent events (default 20)\n"
    "/briefing — latest daily briefing\n"
    "/pause [reason] — stop NEW entries (keeps open positions)\n"
    "/resume — allow new entries again\n"
    "/help — this message\n\n"
    "Or just ask in plain language (e.g. \"how did we do today?\", \"stop buying\")."
)


# ---------------------------------------------------------------------------
# Telegram transport (long polling)
# ---------------------------------------------------------------------------

def _get_updates(token: str, offset: int | None, timeout: int = 50) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    url = _API.format(token=token, method="getUpdates") + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout + 15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError:
        return []
    except (json.JSONDecodeError, ValueError):
        return []
    if not data.get("ok"):
        return []
    return data.get("result", [])


# ---------------------------------------------------------------------------
# Formatting tool results into Telegram text
# ---------------------------------------------------------------------------

def _fmt_status(s: dict[str, Any]) -> str:
    lines = [
        f"Mode: {s.get('mode')}  |  {'PAUSED' if s.get('paused') else 'RUNNING'}",
        f"Equity: {s.get('equity')}",
        f"Open positions: {s.get('open_positions')}",
    ]
    if s.get("paused") and s.get("pause_reason"):
        lines.append(f"Pause reason: {s['pause_reason']}")
    for p in s.get("positions", []):
        lines.append(
            f"  • {p['symbol']} legs={p['legs']}/{p['max_legs']} "
            f"avg={p['avg_entry']} trail={'armed@'+str(p['stop_price']) if p['trail_armed'] else 'off'}"
        )
    return "\n".join(lines)


def _fmt_pnl(d: dict[str, Any]) -> str:
    t = d.get("trades", {})
    e = d.get("events", {})
    return (
        f"P&L last {d.get('window_hours')}h:\n"
        f"  trades: {t.get('count', 0)} (W {t.get('wins', 0)} / L {t.get('losses', 0)})\n"
        f"  net P&L: {t.get('total_pnl', 0)}  fees: {t.get('total_fees', 0)}\n"
        f"  errors: {e.get('error_count', 0)}  "
        f"risk-blocked hits: {e.get('blocked_by_risk_status_hits', 0)}"
    )


def _fmt_trades(d: dict[str, Any]) -> str:
    rows = d.get("trades", [])
    if not rows:
        return "No trades yet."
    out = ["Recent trades:"]
    for t in rows[-10:]:
        when = str(t.get("closed_at", ""))[:16].replace("T", " ")
        out.append(
            f"  {when} {t.get('symbol')} {t.get('side')} "
            f"pnl={t.get('pnl')} ({t.get('exit_reason')})"
        )
    return "\n".join(out)


def _fmt_positions(d: dict[str, Any]) -> str:
    rows = d.get("positions", [])
    if not rows:
        return "No open positions."
    out = ["Open positions:"]
    for p in rows:
        out.append(
            f"  {p['symbol']} legs={p['legs']}/{p['max_legs']} "
            f"size={p['size']} avg={p['avg_entry']} "
            f"trail={'armed@'+str(p['stop_price']) if p['trail_armed'] else 'off'}"
        )
    return "\n".join(out)


def _fmt_events(d: dict[str, Any]) -> str:
    rows = d.get("events", [])
    if not rows:
        return "No events yet."
    out = ["Recent events:"]
    for e in rows[-20:]:
        when = str(e.get("ts", ""))[:19].replace("T", " ")
        extra = e.get("exit_reason") or e.get("reason") or e.get("message") or ""
        sym = e.get("symbol", "")
        out.append(f"  {when} {e.get('event')} {sym} {extra}".rstrip())
    return "\n".join(out)


def _fmt_briefing(d: dict[str, Any]) -> str:
    if not d.get("available"):
        return "No briefing has been generated yet."
    return f"Latest briefing ({d.get('ts', '')[:16].replace('T', ' ')} UTC):\n\n{d.get('briefing_text')}"


def _fmt_result(tool: str, result: dict[str, Any]) -> str:
    formatter = {
        "get_status": _fmt_status,
        "get_pnl": _fmt_pnl,
        "get_recent_trades": _fmt_trades,
        "get_open_positions": _fmt_positions,
        "get_recent_events": _fmt_events,
        "get_last_briefing": _fmt_briefing,
    }.get(tool)
    if formatter:
        return formatter(result)
    if tool == "pause_trading":
        return f"Trading PAUSED.{(' Reason: ' + result.get('reason')) if result.get('reason') else ''}"
    if tool == "resume_trading":
        return "Trading RESUMED."
    return json.dumps(result, indent=2, default=str)


# ---------------------------------------------------------------------------
# Command + natural-language routing
# ---------------------------------------------------------------------------

def _handle_slash(settings: Settings, text: str) -> str | None:
    parts = text.strip().split()
    cmd = parts[0].lstrip("/").lower().split("@")[0]
    arg = parts[1] if len(parts) > 1 else None

    def _int(default: int) -> int:
        try:
            return int(arg) if arg is not None else default
        except ValueError:
            return default

    if cmd in ("start", "help"):
        return HELP_TEXT
    if cmd == "status":
        return _fmt_result("get_status", agent_tools.get_status(settings))
    if cmd == "pnl":
        return _fmt_result("get_pnl", agent_tools.get_pnl(settings, hours=_int(24)))
    if cmd == "trades":
        return _fmt_result("get_recent_trades", agent_tools.get_recent_trades(settings, limit=_int(10)))
    if cmd == "positions":
        return _fmt_result("get_open_positions", agent_tools.get_open_positions(settings))
    if cmd == "events":
        return _fmt_result("get_recent_events", agent_tools.get_recent_events(settings, limit=_int(20)))
    if cmd == "briefing":
        return _fmt_result("get_last_briefing", agent_tools.get_last_briefing(settings))
    if cmd == "pause":
        reason = " ".join(parts[1:]) if len(parts) > 1 else ""
        return _fmt_result("pause_trading", agent_tools.pause_trading(settings, reason=reason, by="telegram"))
    if cmd == "resume":
        return _fmt_result("resume_trading", agent_tools.resume_trading(settings, by="telegram"))
    return f"Unknown command /{cmd}. Send /help for the list."


def _route_with_gemini(settings: Settings, text: str) -> str:
    """Ask Gemini to map free text to a single tool call, then execute it."""
    if not settings.gemini_api_key:
        return "I didn't recognize that command. Send /help.\n(Natural language needs GEMINI_API_KEY.)"

    prompt = (
        "You are the router for a crypto trading bot's Telegram control channel.\n"
        "Given the user's message, choose AT MOST ONE tool to call from this catalog:\n\n"
        f"{agent_tools.tools_catalog_text()}\n\n"
        "Rules:\n"
        "- You may ONLY use the tools above. You cannot open, close, or modify positions.\n"
        "- If the user wants to stop/halt/pause buying, use pause_trading.\n"
        "- If the user wants to resume/continue/start, use resume_trading.\n"
        "- Respond with STRICT JSON only, no prose, no code fences.\n"
        '- Format: {"tool": "<name>", "args": {<kwargs>}} to call a tool, '
        'or {"reply": "<text>"} if no tool fits.\n\n'
        f"User message: {text!r}\n"
    )
    raw = _gemini_generate(settings.gemini_api_key, prompt).strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        decision = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return "Sorry, I couldn't interpret that. Try /help."

    if "tool" in decision:
        tool = decision["tool"]
        args = decision.get("args") or {}
        try:
            result = agent_tools.call_tool(settings, tool, args, by="telegram")
        except KeyError:
            return f"(Router picked an unavailable tool: {tool}.) Try /help."
        except Exception as exc:  # noqa: BLE001
            return f"Tool {tool} failed: {exc}"
        return _fmt_result(tool, result)
    return str(decision.get("reply", "I'm not sure how to help with that. Try /help."))


def handle_message(settings: Settings, text: str) -> str:
    text = (text or "").strip()
    if not text:
        return HELP_TEXT
    if text.startswith("/"):
        return _handle_slash(settings, text) or HELP_TEXT
    return _route_with_gemini(settings, text)


# ---------------------------------------------------------------------------
# Listener loop
# ---------------------------------------------------------------------------

def run_listener(settings: Settings) -> None:
    if not settings.telegram_control_configured:
        raise RuntimeError(
            "Telegram control requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
        )
    token = settings.telegram_bot_token
    authorized = str(settings.telegram_chat_id).strip()
    print(f"[telegram-control] listening (authorized chat id: {authorized})")
    offset: int | None = None
    while True:
        try:
            updates = _get_updates(token, offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "")
                if chat_id != authorized:
                    print(f"[telegram-control] ignoring message from unauthorized chat {chat_id}")
                    continue
                if not text:
                    continue
                try:
                    reply = handle_message(settings, text)
                except Exception as exc:  # noqa: BLE001
                    reply = f"Error handling that: {exc}"
                _telegram_send(token, authorized, reply)
        except Exception as exc:  # noqa: BLE001 -- keep the loop alive
            print(f"[telegram-control] ERROR {exc}")
            time.sleep(5)


def start_control_thread(settings: Settings):
    """Start the listener in a daemon thread (used by ``src/main.py``)."""
    import threading  # noqa: PLC0415

    if not settings.telegram_control_enabled or not settings.telegram_control_configured:
        return None
    t = threading.Thread(target=run_listener, args=(settings,), name="telegram-control", daemon=True)
    t.start()
    return t


def main() -> None:
    settings = load_settings()
    if not settings.telegram_control_configured:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
    run_listener(settings)


if __name__ == "__main__":
    main()
