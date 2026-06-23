# Hyperliquid Futures Bot — Agentic & Paper-First

Automated crypto futures bot for Hyperliquid perpetual swaps (default
**BTC/USDC:USDC** and **ETH/USDC:USDC**). Runs in **paper mode** by default;
live trading is gated behind credentials. Every agent surface (web dashboard,
MCP server, two-way Telegram) is read + pause/resume only — agents can **never**
open, close, or modify a position.

## Strategies

The bot ships with two interchangeable, **direction-aware** strategies, selected
by `STRATEGY` in `.env`. A symbol listed in `SHORT_SYMBOLS` trades the mirror
(short) logic; everything else is long.

### `STRATEGY=dca` — dip-buying DCA (mean-reversion)

| Step | Detail |
|------|--------|
| Entry (long) | Open the first leg when a recent bar dipped ≥ `INITIAL_DIP_PCT` below the local high, confirmed by a green bar; gated by the EMA trend filter and per-symbol price cap. |
| DCA add | Add a leg when price falls a further `DCA_TRIGGER_PCT` below the last fill (up to `MAX_DCA_LEGS`). |
| Exit | Percent trailing stop (`TRAIL_ACTIVATE_PCT` to arm, `TRAIL_DISTANCE_PCT` to trail), plus optional hard `STOP_LOSS_PCT` and `TAKE_PROFIT_PCT`. |
| Trend filter | When `TREND_FILTER_ENABLED`, only trade with price on the right side of the `TREND_EMA_PERIOD` EMA. |

### `STRATEGY=trend` — EMA/ATR trend-following (recommended)

| Step | Detail |
|------|--------|
| Entry (long) | `FAST_EMA` > `SLOW_EMA` **and** price > `TREND_EMA_PERIOD` (regime) EMA. |
| Initial stop | `STOP_ATR_MULTIPLIER` × ATR from entry. |
| Trailing stop | ATR "chandelier": `TRAIL_ATR_MULTIPLIER` × ATR from the favorable extreme (wide = let winners run). |
| Soft exit | Close when the fast/slow EMA cross flips against the position. |
| Sizing | Fixed-fractional: risk `RISK_PER_TRADE_PCT` of equity per trade (size = risk ÷ stop distance), capped by `MAX_LEVERAGE`. |

Backtest (22 months, 4h BTC/ETH, `STRATEGY=trend`): **+11.7%** return, **1.27**
profit factor, **16.4%** max drawdown — a ~35% win rate with average win ≈ 2.4×
average loss. Backtested, not live; figures depend on the window and parameters.

### Entry-quality filters (opt-in)

Four filters can tighten entries on top of either strategy. **All are disabled by
default**, so the backtested figures above and existing behaviour are unchanged
until you turn one on. They live in the shared strategy functions, so the live
bot and the backtester apply them identically.

| Filter | Knob(s) | Rule | Applies to |
|--------|---------|------|------------|
| **ADX chop filter** | `ADX_MIN` (0=off), `ADX_PERIOD` | Skip entries unless Wilder ADX ≥ `ADX_MIN` (e.g. 25) — stand aside in ranging markets | `trend` only |
| **Volume confirmation** | `VOLUME_MIN_MULT` (0=off), `VOLUME_MA_PERIOD` | Entry bar volume must exceed `VOLUME_MIN_MULT` × the volume average (e.g. 1.5×) | both |
| **MTF alignment** | `MTF_ENABLED`, `MTF_TIMEFRAME`, `MTF_EMA_PERIOD` | Longs only when the higher timeframe closes above its EMA (mirror for shorts) | both |
| **DCA chandelier** | `DCA_CHANDELIER_ENABLED` | Use an ATR chandelier trail (`TRAIL_ATR_MULTIPLIER` × ATR from the extreme) for the DCA exit instead of the percent trail | `dca` only |

Backtesting the MTF filter needs cached higher-timeframe candles too:
`python -m src.fetch_candles --timeframes 4h`. The backtester errors with a clear
hint if `MTF_ENABLED=true` but the HTF data file is missing.

### Startup state reconciliation (live mode)

On startup, `LiveBroker` queries Hyperliquid for any open positions and adopts
them into its internal state before trading resumes. This prevents a restart
(crash, deploy, reboot) from treating an account with live exposure as flat and
opening a duplicate position. Trail state is reset and re-ratchets from live
candles on subsequent ticks.

## Risk Engine

| Guard | Trigger | Action |
|-------|---------|--------|
| Per-trade sizing | strategy sizing (DCA leg notional or trend risk %) | Caps position size |
| Max leverage | Notional > `MAX_LEVERAGE` x equity | Caps position size |
| Consecutive losses | Streak >= `MAX_CONSECUTIVE_LOSSES` | Halt all trading for `CONSEC_HALT_HOURS` |
| Rolling drawdown | Drawdown from window start >= `MAX_DAILY_LOSS_PCT` | Halt all trading for `DAILY_LOSS_HALT_HOURS` |

Halts are **timer-based** -- the bot automatically resumes after the configured hours. The risk window (equity baseline + loss streak) resets when a halt expires.

## Quick Start

### 1. Python environment

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure

```powershell
copy .env.example .env
```

Edit `.env` -- at minimum set `MODE=paper` and review the strategy / risk parameters.
For Hyperliquid, credentials (`HL_WALLET_ADDRESS`, `HL_PRIVATE_KEY`) are only required for live trading; public market data works without them.

### 3. Fetch historical data (for backtesting)

```powershell
python -m src.fetch_candles --days 45
```

This downloads OHLCV candles to `data/` for the configured timeframe(s) used by the backtester.

### 4. Backtest

```powershell
python -m src.backtest --env .env
python -m src.backtest --env .env --set STRATEGY=trend TIMEFRAME=4h LOOKBACK_CANDLES=400
python -m src.backtest --env .env --set STRATEGY=dca STOP_LOSS_PCT=30 TAKE_PROFIT_PCT=20
```

### 5. Run the bot

```powershell
python -m src.main
```

The bot polls Hyperliquid every `POLL_SECONDS`, generates signals, simulates orders in paper mode, and writes logs to `logs/events.jsonl` and `logs/trades.jsonl`.

### 6. Daily Telegram briefing (optional)

The bot can send a **performance and risk summary** to Telegram, generated by the **Gemini API** from the last **24 hours** of `logs/trades.jsonl` and `logs/events.jsonl` (aggregated P&L, fees, heartbeat equity, errors, and `blocked_by_risk` hints from heartbeats).

**Configure** in `.env` (see `.env.example`): set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `GEMINI_API_KEY`. In Telegram, open your bot and send `/start` once, then set `TELEGRAM_CHAT_ID` to your numeric user id (for example from `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` after messaging the bot, or via @userinfobot).

**Send once (manual or cron):**

```powershell
python -m src.briefing
```

**Schedule inside the running bot:** set `DAILY_BRIEFING_ENABLED=true`. A background thread sends one briefing per **UTC calendar day** at `DAILY_BRIEFING_HOUR_UTC` (0–23). State is stored in `logs/briefing_state.json` so the same day is not sent twice after a restart.

Optional: `GEMINI_MODEL` (default in code: `gemini-2.0-flash` if unset) overrides the Gemini model name.

If the logs contain no trades or heartbeats for that window, the summary will reflect **empty data** — run the bot so `events.jsonl` receives heartbeats and completed trades appear in `trades.jsonl`.

### 7. Web dashboard (optional)

A read-only monitoring UI (FastAPI + Chart.js) that renders live equity, open positions, recent trades/events, pause state, and the latest briefing. It reads the same `logs/*.jsonl` the bot writes, so it runs as a **separate process**.

```powershell
python -m src.webapp
```

Then open `http://<host>:<DASHBOARD_PORT>` (default `8000`). Configure `DASHBOARD_HOST` / `DASHBOARD_PORT` in `.env`. The dashboard is **read-only** — it has no endpoint that can trade, pause, or change anything.

**Public demo (sample data, no real bot logs):** set `DASHBOARD_DEMO_MODE=true`. The dashboard then reads committed files under `demo_logs/` (fake trades, heartbeats, briefing) and shows a **DEMO** banner. Safe to expose on a public URL without leaking your real performance data. The trading bot does not need to be running on that VM.

### 8. Two-way Telegram control (optional, agentic)

Turns the one-way briefing into an interactive control channel. You message the bot; it runs a **slash command** directly, or routes **free-text** through Gemini to the right tool.

```powershell
python -m src.telegram_control
```

Or set `TELEGRAM_CONTROL_ENABLED=true` to run it inside `python -m src.main`. Commands: `/status`, `/pnl [hours]`, `/trades [n]`, `/positions`, `/events [n]`, `/briefing`, `/pause [reason]`, `/resume`, `/help`. Only messages from your `TELEGRAM_CHAT_ID` are honored.

> **Safety boundary:** the agent can read stats and **pause/resume** trading only. It can **never open, close, or modify a position**, and cannot switch to live mode. Pausing blocks *new* entries and DCA adds; open positions keep their trailing-stop protection. This is enforced by construction — the shared tool registry (`src/agent_tools.py`) contains no order-placement tool.

### 9. MCP server (optional, agentic)

Exposes the same read + pause/resume tools over the **Model Context Protocol**, so any MCP client (Cursor, Claude Desktop, a custom agent) can query and control the bot through a standard interface.

```powershell
python -m src.mcp_server
```

Example MCP client config:

```json
{
  "mcpServers": {
    "crypto-bot": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/path/to/crypto-trade-bot"
    }
  }
}
```

## Environment Variables

All variables are loaded from `.env` via `python-dotenv`. See `.env.example` for a complete template.

### Connection

| Variable | Default | Description |
|----------|---------|-------------|
| `MODE` | `paper` | `paper` (simulated fills) or `live` (real orders; requires credentials) |
| `HL_WALLET_ADDRESS` | | Your 0x… EVM wallet address |
| `HL_PRIVATE_KEY` | | Hex private key for the wallet |
| `HL_TESTNET` | `false` | Use Hyperliquid testnet (`true` / `false`) |

### Market

| Variable | Default | Description |
|----------|---------|-------------|
| `SYMBOLS` | `BTC/USDC:USDC,ETH/USDC:USDC` | Comma-separated perpetual symbols (base coin before `/`, e.g. `BTC`) |
| `SHORT_SYMBOLS` | (empty) | Bases traded short-only (mirror logic); others are long. Empty = long-only |
| `STRATEGY` | `dca` | `dca` (dip-buying) or `trend` (EMA/ATR trend-following) |
| `TIMEFRAME` | `15m` | Primary candle timeframe (use `4h` for `STRATEGY=trend`) |
| `POLL_SECONDS` | `30` | Seconds between each polling loop |
| `LOOKBACK_CANDLES` | `200` | Number of candles fetched for indicator calculation |
| `HEARTBEAT_INTERVAL` | `5` | Print/log heartbeat every N loops |

### Position Sizing

| Variable | Default | Description |
|----------|---------|-------------|
| `INITIAL_EQUITY` | `10000` | Starting paper equity (USD) |
| `MAX_LEVERAGE` | `3` | Max notional / equity ratio |

### Risk Guards

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_DAILY_LOSS_PCT` | `2.0` | Rolling drawdown % that triggers a halt |
| `MAX_CONSECUTIVE_LOSSES` | `3` | Loss streak that triggers a halt |
| `CONSEC_HALT_HOURS` | `6` | Hours to pause after consecutive-loss halt |
| `DAILY_LOSS_HALT_HOURS` | `12` | Hours to pause after drawdown halt |

### Strategy — DCA (`STRATEGY=dca`)

| Variable | Default | Description |
|----------|---------|-------------|
| `LONG_MAX_PRICES` | `BTC:90000,ETH:3000` | Per-symbol long-entry price ceiling (shorts ignore it) |
| `INITIAL_DIP_PCT` | `3.0` | % drop from the recent high to open the first leg |
| `DCA_TRIGGER_PCT` | `10.0` | % further move past the last fill to add a leg |
| `LEG_NOTIONAL_PCT` | `10.0` | % of starting equity per leg |
| `MAX_DCA_LEGS` | `5` | Max legs per symbol |
| `TRAIL_ACTIVATE_PCT` | `5.0` | Arm the trailing stop once this far in profit |
| `TRAIL_DISTANCE_PCT` | `3.0` | Trail distance from the favorable extreme |
| `STOP_LOSS_PCT` | `0` | Hard stop (0 = disabled) |
| `TAKE_PROFIT_PCT` | `0` | Fixed take-profit (0 = disabled) |
| `TREND_FILTER_ENABLED` | `true` | Only trade with the `TREND_EMA_PERIOD` EMA trend |
| `TREND_EMA_PERIOD` | `200` | EMA period for the trend / regime filter (shared with trend mode) |

### Strategy — Trend-following (`STRATEGY=trend`, best on `TIMEFRAME=4h`)

| Variable | Default | Description |
|----------|---------|-------------|
| `FAST_EMA` | `50` | Fast EMA for the trend cross |
| `SLOW_EMA` | `200` | Slow EMA for the trend cross |
| `ATR_PERIOD` | `14` | ATR lookback for stops/sizing |
| `STOP_ATR_MULTIPLIER` | `3.0` | Initial stop = entry -/+ this × ATR |
| `TRAIL_ATR_MULTIPLIER` | `6.0` | Chandelier trail = extreme -/+ this × ATR |
| `RISK_PER_TRADE_PCT` | `1.0` | % of equity risked per trade (sets size from stop distance) |

### Entry-quality filters (opt-in, off by default)

| Variable | Default | Description |
|----------|---------|-------------|
| `ADX_MIN` | `0` | Min Wilder ADX to allow `trend` entries (e.g. `25`); `0` disables |
| `ADX_PERIOD` | `14` | ADX lookback |
| `VOLUME_MIN_MULT` | `0` | Entry bar volume must exceed this × the volume average (e.g. `1.5`); `0` disables |
| `VOLUME_MA_PERIOD` | `20` | Lookback for the volume average |
| `MTF_ENABLED` | `false` | Require higher-timeframe trend alignment for entries |
| `MTF_TIMEFRAME` | `4h` | Higher timeframe for the MTF filter |
| `MTF_EMA_PERIOD` | `50` | EMA period on the higher timeframe |
| `DCA_CHANDELIER_ENABLED` | `false` | Use an ATR chandelier trail for the `dca` exit instead of the percent trail |

### Execution

| Variable | Default | Description |
|----------|---------|-------------|
| `ENTRY_FEE_BPS` | `2` | Entry fee in basis points (limit/maker) |
| `EXIT_FEE_BPS` | `3.5` | Exit fee in basis points (market/taker) |
| `SLIPPAGE_BPS` | `2` | Simulated slippage on exits |
| `LIMIT_TIMEOUT_SECONDS` | `30` | Cancel unfilled limit orders after this many seconds |

### Daily briefing (Telegram + Gemini)

| Variable | Default | Description |
|----------|---------|-------------|
| `DAILY_BRIEFING_ENABLED` | (off) | Set `true` / `1` / `yes` to run the in-process daily scheduler while `python -m src.main` is running |
| `TELEGRAM_BOT_TOKEN` | | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | | Destination chat id (user or group; message the bot `/start` first for private chats) |
| `GEMINI_API_KEY` | | Google AI / Gemini API key |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Model id if you need another Gemini model |
| `DAILY_BRIEFING_HOUR_UTC` | `8` | UTC hour (0–23) for the in-process daily send |

### Dashboard & agentic control

| Variable | Default | Description |
|----------|---------|-------------|
| `DASHBOARD_HOST` | `0.0.0.0` | Bind address for the web dashboard (`127.0.0.1` for local-only) |
| `DASHBOARD_PORT` | `8000` | Port for the web dashboard |
| `DASHBOARD_DEMO_MODE` | (off) | Set `true` for a public demo using fake data from `demo_logs/` |
| `TELEGRAM_CONTROL_ENABLED` | (off) | Set `true` to run the two-way Telegram listener inside `python -m src.main` |

The Telegram control listener and MCP server reuse `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `GEMINI_API_KEY` from the briefing section above.

## Project Layout

```
src/
  main.py           Entry point -- loads settings, runs bot (+ optional briefing/control threads)
  bot.py            Main polling loop and per-symbol processing (DCA + trend paths)
  exchange.py       Hyperliquid official SDK adapter (with retry) + paper / live broker
  strategy.py       STRATEGY=dca: dip-buying / DCA / trail / SL / TP (direction-aware)
  strategy_trend.py STRATEGY=trend: EMA cross + regime, ATR stops, chandelier trail, risk sizing
  risk.py           Position sizing and timer-based halt logic
  config.py         Loads Settings from .env
  models.py         Shared types: Position, TradeResult, PendingOrder
  backtest.py       Offline backtester on cached candle data
  fetch_candles.py  Download & cache OHLCV from Hyperliquid
  briefing.py       Daily Telegram summary via Gemini (also: python -m src.briefing)
  control.py        Cross-process pause/resume flag (logs/control.json)
  agent_tools.py    Single registry of agent-callable tools (read + pause/resume only)
  webapp.py         Read-only FastAPI dashboard (python -m src.webapp)
  mcp_server.py     MCP server exposing the tool registry (python -m src.mcp_server)
  telegram_control.py  Two-way Telegram control listener (python -m src.telegram_control)
  static/
    index.html      Dashboard single-page UI (Chart.js)
demo_logs/
  *.jsonl           Sample trades/events/briefings for public dashboard demo
  control.json      Sample pause state for demo mode
logs/
  events.jsonl      Heartbeats, position opens/closes, errors
  trades.jsonl      Completed trade records with P&L
  briefings.jsonl   Full daily briefing history
  briefing_state.json  Last UTC date the in-process daily briefing was sent (optional)
  control.json      Pause/resume state shared across processes (optional)
data/
  *.json            Cached OHLCV candle data for backtesting
```

## Deployment

See [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md) for a step-by-step guide to run the bot 24/7 on a GCP VM with systemd auto-restart and log rotation.

## Warning

This is educational software, not financial advice. Futures trading with leverage can result in rapid losses. Always test in paper mode first and use small sizes when transitioning to live.
