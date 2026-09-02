"""MCP (Model Context Protocol) server for the trading bot.

Exposes the bot's read + control capabilities as MCP **tools** so any
MCP-compatible client (Cursor, Claude Desktop, a custom agent) can query the
bot and pause/resume it through a standard protocol.

The tool set mirrors ``src/agent_tools.py`` exactly:

    READ     get_status, get_pnl, get_recent_trades, get_open_positions,
             get_recent_events, get_last_briefing
    CONTROL  pause_trading, resume_trading

There is **no** tool to open, close, or modify a position, and none to switch
to live mode -- by design the agent can never place an order.

Run (stdio transport, the usual way an MCP client launches a server):
    python -m src.mcp_server

Example Cursor / Claude Desktop config entry:
    {
      "mcpServers": {
        "crypto-bot": {
          "command": "/path/to/.venv/bin/python",
          "args": ["-m", "src.mcp_server"],
          "cwd": "/path/to/crypto-trade-bot"
        }
      }
    }
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import agent_tools
from .config import load_settings

_settings = load_settings()
mcp = FastMCP("crypto-bot")


# --- READ tools ------------------------------------------------------------

@mcp.tool()
def get_status() -> dict[str, Any]:
    """Current bot status: mode, equity, open-position count, and pause state."""
    return agent_tools.get_status(_settings)


@mcp.tool()
def get_pnl(hours: int = 24) -> dict[str, Any]:
    """Aggregated P&L, win/loss counts, fees and risk events over the last N hours."""
    return agent_tools.get_pnl(_settings, hours=hours)


@mcp.tool()
def get_recent_trades(limit: int = 10) -> dict[str, Any]:
    """List the most recent closed trades with entry/exit prices and P&L."""
    return agent_tools.get_recent_trades(_settings, limit=limit)


@mcp.tool()
def get_open_positions() -> dict[str, Any]:
    """List currently open positions (symbol, legs, average entry, trail state)."""
    return agent_tools.get_open_positions(_settings)


@mcp.tool()
def get_recent_events(limit: int = 20) -> dict[str, Any]:
    """Recent notable events: opens, trailing-stop exits, errors."""
    return agent_tools.get_recent_events(_settings, limit=limit)


@mcp.tool()
def get_last_briefing() -> dict[str, Any]:
    """The text of the most recent daily Gemini briefing, if one exists."""
    return agent_tools.get_last_briefing(_settings)


# --- CONTROL tools (pause / resume ONLY) -----------------------------------

@mcp.tool()
def pause_trading(reason: str = "") -> dict[str, Any]:
    """Pause NEW entries. Does NOT close or modify open positions."""
    return agent_tools.pause_trading(_settings, reason=reason, by="mcp")


@mcp.tool()
def resume_trading() -> dict[str, Any]:
    """Resume normal trading after a pause. Open positions are unaffected."""
    return agent_tools.resume_trading(_settings, by="mcp")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
