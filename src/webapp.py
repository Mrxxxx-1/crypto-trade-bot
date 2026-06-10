"""Read-only web dashboard for the trading bot.

A small FastAPI app that renders the bot's live state in a browser: equity
curve, open positions, recent trades, recent events, pause state, and the
latest Gemini briefing.  It reads the same ``logs/*.jsonl`` files the bot
writes, so it can run as a **separate process** (its own systemd service) on
the same machine without touching the bot.

The dashboard is intentionally **read-only** -- it exposes no endpoint that can
trade, pause, or change anything.  All control happens through the Telegram /
MCP agent layer, which is gated to the owner.

Run:
    python -m src.webapp
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import agent_tools
from .briefing import _read_jsonl
from .config import Settings, load_settings

_STATIC_DIR = Path(__file__).parent / "static"


def _equity_curve(settings: Settings, max_points: int = 1000) -> list[dict[str, Any]]:
    """Equity over time from closed trades + heartbeats, sorted by timestamp."""
    logs = Path(settings.logs_dir)
    points: list[tuple[str, float]] = []

    for row in _read_jsonl(logs / "trades.jsonl"):
        ts = row.get("closed_at")
        eq = row.get("equity")
        if ts and eq is not None:
            try:
                points.append((str(ts), float(eq)))
            except (TypeError, ValueError):
                continue

    for row in _read_jsonl(logs / "events.jsonl"):
        if row.get("event") != "heartbeat":
            continue
        ts = row.get("ts")
        eq = row.get("equity")
        if ts and eq is not None:
            try:
                points.append((str(ts), float(eq)))
            except (TypeError, ValueError):
                continue

    points.sort(key=lambda p: p[0])
    if len(points) > max_points:
        step = len(points) // max_points + 1
        points = points[::step]
    return [{"ts": ts, "equity": round(eq, 2)} for ts, eq in points]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="Crypto Bot Dashboard", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        return JSONResponse(agent_tools.get_status(settings))

    @app.get("/api/pnl")
    def api_pnl(hours: int = 24) -> JSONResponse:
        return JSONResponse(agent_tools.get_pnl(settings, hours=hours))

    @app.get("/api/trades")
    def api_trades(limit: int = 20) -> JSONResponse:
        return JSONResponse(agent_tools.get_recent_trades(settings, limit=limit))

    @app.get("/api/positions")
    def api_positions() -> JSONResponse:
        return JSONResponse(agent_tools.get_open_positions(settings))

    @app.get("/api/events")
    def api_events(limit: int = 30) -> JSONResponse:
        return JSONResponse(agent_tools.get_recent_events(settings, limit=limit))

    @app.get("/api/briefing")
    def api_briefing() -> JSONResponse:
        return JSONResponse(agent_tools.get_last_briefing(settings))

    @app.get("/api/equity")
    def api_equity() -> JSONResponse:
        return JSONResponse({"points": _equity_curve(settings)})

    return app


def main() -> None:
    import uvicorn  # noqa: PLC0415 -- optional heavy import

    settings = load_settings()
    print(
        f"Dashboard on http://{settings.dashboard_host}:{settings.dashboard_port} "
        f"(reading {settings.logs_dir}/)"
    )
    uvicorn.run(
        create_app(settings),
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
