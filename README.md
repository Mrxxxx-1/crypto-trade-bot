# Hyperliquid Futures Bot — Agentic & Paper-First

Automated crypto futures bot for Hyperliquid perpetual swaps (default
**BTC/USDC:USDC** and **ETH/USDC:USDC**). Runs in **paper mode** by default;
live trading is gated behind credentials. Every agent surface (web dashboard,
MCP server, two-way Telegram) is read + pause/resume only — agents can **never**
open, close, or modify a position.

## Strategies

`STRATEGY` in `.env` selects between one directional strategy and a manual
hedge-only mode:

| `STRATEGY` | What it does |
|-----------|--------------|
| `trend` (default) | EMA/ATR trend-following, described below. The only strategy that trades on its own. |
| `hedge` | Trades nothing. Services only the [manual catalyst hedge](#catalyst-hedge-manual-and-opt-in). |

A symbol listed in `SHORT_SYMBOLS` trades the mirror (short) logic; everything
else is long. You can instead set
[`DIRECTION_MODE=signal`](#choosing-a-direction-direction_mode) to let the trend
choose the side per bar rather than pinning it per symbol.

> A previous dip-buying DCA strategy (`STRATEGY=dca`) has been removed. An old
> `.env` still carrying `STRATEGY=dca` fails at startup with a message pointing
> here rather than silently trading something unintended.

### `STRATEGY=trend` — EMA/ATR trend-following

| Step | Detail |
|------|--------|
| Entry (long) | `FAST_EMA` > `SLOW_EMA` **and** price > `TREND_EMA_PERIOD` (regime) EMA. |
| Entry (short) | The exact mirror: `FAST_EMA` < `SLOW_EMA` **and** price < the regime EMA. |
| Initial stop | `STOP_ATR_MULTIPLIER` × ATR from entry — below for longs, above for shorts. |
| Trailing stop | ATR "chandelier": `TRAIL_ATR_MULTIPLIER` × ATR from the favorable extreme (highest high for longs, lowest low for shorts; wide = let winners run). |
| Soft exit | Close when the fast/slow EMA cross flips against the position. |
| Sizing | Fixed-fractional: risk `RISK_PER_TRADE_PCT` of equity per trade (size = risk ÷ stop distance), capped by `MAX_LEVERAGE`. |

#### Choosing a direction: `DIRECTION_MODE`

By default (`DIRECTION_MODE=static`) `config.direction_for()` pins each symbol to one
direction for the life of the process: short if its base coin is in `SHORT_SYMBOLS`, long
otherwise. A symbol never flips. With `SHORT_SYMBOLS=BTC,ETH` the bot will *only* ever
short BTC and ETH — a bullish EMA cross produces no trade at all, just a
`no_entry(short) no_trend` heartbeat. Setting every symbol you trade to short is therefore
a directional bet on the whole book, and it bleeds through a rally.

`DIRECTION_MODE=signal` instead asks the strategy which way the trend points and trades
that side, ignoring `SHORT_SYMBOLS`. Because the long and short rules are strict mirrors,
at most one can fire; when neither does, the bot stands aside as before.

| `STRATEGY=trend`, 22mo 4h BTC/ETH | Net P&L | Return | Profit factor | Max DD | Trades | Fees |
|------|---------|--------|------|--------|--------|------|
| `static`, `SHORT_SYMBOLS=BTC,ETH` | +$1,752 | +17.5% | 1.35 | 11.2% | 118 | $167 |
| `static`, `SHORT_SYMBOLS=` (long-only) | +$2,727 | +27.3% | 1.58 | 13.0% | 109 | $191 |
| `signal` | **+$5,187** | **+51.9%** | 1.50 | 14.6% | 226 | $387 |

Reproduce with `python -m src.backtest --set STRATEGY=trend TIMEFRAME=4h DIRECTION_MODE=signal`.

Signal mode roughly doubles the trade count, since both sides of every symbol become
tradeable — and doubles the fees with it. The extra return is not free: max drawdown rises
to 14.6% and the worst losing streak grows to 10 trades. It is still trend-following, so
the P&L is concentrated: 10 profitable months against 13 losing ones, and removing the
four best months turns the whole run negative.

Two safety notes:

- **An open position never flips.** Direction is recovered from the position's own `side`
  while it is open, in both modes. Re-resolving mid-trade would invert every stop and
  trail comparison and turn a protective stop into a target. `tests/test_direction.py`
  guards this.
- **A typo raises at startup** rather than falling back, so a mistyped `signal` can't
  silently leave you pinned to `SHORT_SYMBOLS`.

A typo in `DIRECTION_MODE` raises at startup rather than falling back, so a mistyped
`signal` can't silently leave you pinned to `SHORT_SYMBOLS`.

To see which side each symbol would take right now, and which gate is holding an entry
back, run the preflight against your cached candles:

```bash
python scripts/check-direction.py                # uses .env
python scripts/check-direction.py --mode static  # compare, without editing .env
```

```
BTC/USDC:USDC   last=77,910.00
   long signal : True
   short signal: False
   -> direction: long   (static mode would say short)
   atr=872.21  stop=75,293.36  size=0.0038  notional=$297.75
   NO ENTRY - blocked by: ADX >= 25.0 (is 12.3)
```

Backtest (22 months, 4h BTC/ETH, `STRATEGY=trend`): **+11.7%** return, **1.27**
profit factor, **16.4%** max drawdown — a ~35% win rate with average win ≈ 2.4×
average loss. Backtested, not live; figures depend on the window and parameters.

### Entry-quality filters (opt-in)

Three filters can tighten entries. **All are disabled by default**, so the
backtested figures above and existing behaviour are unchanged until you turn one
on. They live in `src/indicators.py`, shared by the live bot and the backtester,
so both apply them identically.

| Filter | Knob(s) | Rule |
|--------|---------|------|
| **ADX chop filter** | `ADX_MIN` (0=off), `ADX_PERIOD` | Skip entries unless Wilder ADX ≥ `ADX_MIN` (e.g. 25) — stand aside in ranging markets |
| **Volume confirmation** | `VOLUME_MIN_MULT` (0=off), `VOLUME_MA_PERIOD` | Entry bar volume must exceed `VOLUME_MIN_MULT` × the volume average (e.g. 1.5×) |
| **MTF alignment** | `MTF_ENABLED`, `MTF_TIMEFRAME`, `MTF_EMA_PERIOD` | Longs only when the higher timeframe closes above its EMA (mirror for shorts) |

Each gate returns "allowed" when its knob is off, and "blocked" when there isn't
enough history to decide — a cold start never enters on thin data.

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
| Per-trade sizing | Risk `RISK_PER_TRADE_PCT` of equity per trade | Caps position size |
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
python -m src.backtest --env .env --set TIMEFRAME=4h LOOKBACK_CANDLES=400
python -m src.backtest --env .env --label SIGNAL --set DIRECTION_MODE=signal
```

Use `--env .env` (not the `.env.example` default) so the run uses your live
parameters. `--set` accepts any setting name; comma-separated lists such as
`SYMBOLS` and `SHORT_SYMBOLS` are parsed as lists.

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

Or set `TELEGRAM_CONTROL_ENABLED=true` to run it inside `python -m src.main`. Commands: `/status`, `/pnl [hours]`, `/trades [n]`, `/positions`, `/events [n]`, `/briefing`, `/pause [reason]`, `/resume`, `/hedge`, `/help`. Only messages from your `TELEGRAM_CHAT_ID` are honored.

> **Safety boundary:** free text routed through Gemini can read stats and **pause/resume** trading only. It can **never open, close, or modify a position**, and cannot switch to live mode. This is enforced by construction — the shared tool registry (`src/agent_tools.py`) contains no order-placement tool, and `tests/test_hedge.py` asserts it stays that way. The single exception is `/hedge arm`, a hard-coded slash command that is deliberately *not* registered as a tool, so no phrasing of a plain-language message can reach it.

### 8b. Catalyst hedge (optional, manual)

A **hedge** opens a mirrored long and short on one coin before a scheduled event (CPI, FOMC, an ETF decision). Whichever stop the market reaches first identifies the losing leg: it is cut for a capped loss, and the survivor trails.

> **Read this before enabling it.** A hedge cannot generate profit on its own. The loser's realized loss always equals the winner's unrealized gain at the moment of the cut, in every price path, so the pair is exactly equivalent to a single position entered at the cut price — minus an extra round trip in fees. Its one real benefit is that the survivor's cost basis is fixed the instant the hedge opens, so a violent catalyst gap cannot slip your entry. That is worth a few bps for a known event and nothing at all as a daily strategy: backtesting the automated version (arming on volatility compression) dropped profit factor below 1, which is why entry is manual.

**Setup.** Hyperliquid holds one net position per coin, so the two legs need two accounts. Sub-accounts have no key of their own; you sign with the master or an approved API wallet and set `vaultAddress`.

1. **Create a sub-account** in the Hyperliquid UI (Portfolio → Sub-Accounts). An API wallet *cannot* do this — it is an owner-level action.
2. **Fund it** by transferring USDC from the main account, again in the UI. An even split gives both legs the same margin.
3. **Approve a second API wallet** on the master and put its key in `HEDGE_SUB_PRIVATE_KEY`. Hyperliquid tracks nonces per signer, and the hedge fires both legs simultaneously, so sharing one key between them risks a nonce collision.
4. **Verify** — this checks every precondition and prints the fix for each failure:

```powershell
python -m src.subaccount status      # addresses, roles, sub-accounts, balances
python -m src.subaccount preflight   # go / no-go with remediation steps
```

5. Set `HEDGE_ENABLED=true` and `HEDGE_SUB_ACCOUNT=0x...`, then restart the bot.

**Use.**

```
/hedge arm BTC CPI print    # request a hedge; the bot opens it on its next poll
/hedge                      # status: legs, stops, realized P&L
/hedge close                # flatten immediately
```

Only one hedge may be active at a time. An armed request that never opens expires after `HEDGE_EXPIRY_HOURS`; an opened hedge that never triggers auto-closes after `HEDGE_MAX_HOURS`.

**Sizing note.** `HEDGE_ATR_FLOOR_PCT` floors the reference ATR at a percentile of its own recent history. This exists because catalysts arrive precisely when volatility is compressed, and backtesting showed that stops sized off a squeezed ATR get *both* legs whipsawed by the expansion — the average winner collapsed from $4.88 to $1.19. The floor keeps the stop wide enough to survive the move the hedge exists to capture.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HEDGE_ENABLED` | `false` | Master switch. Every hedge code path is inert when false. |
| `HEDGE_SUB_ACCOUNT` | — | Sub-account address holding the short leg. |
| `HEDGE_SUB_PRIVATE_KEY` | — | Second API wallet for the short leg (avoids nonce collisions). |
| `HEDGE_SYMBOLS` | `SYMBOLS` | Coins the hedge may be armed on. |
| `HEDGE_RISK_PCT` | `0.5` | Percent of combined equity risked per leg — this is the capped loss. |
| `HEDGE_STOP_ATR_MULT` | `2.0` | Initial stop distance in reference ATRs. |
| `HEDGE_TRAIL_ATR_MULT` | `4.0` | Winner's trailing distance in reference ATRs. |
| `HEDGE_ATR_FLOOR_PCT` | `50.0` | Percentile floor on the reference ATR; `0` disables. |
| `HEDGE_MAX_HOURS` | `48` | Auto-close an untriggered hedge; `0` disables. |
| `HEDGE_EXPIRY_HOURS` | `12` | An armed-but-unopened request expires. |

#### The $100k sub-account gate

Hyperliquid will not let an account create a sub-account until it has traded **$100,000 of lifetime volume**. If `subaccount preflight` reports `sub_account_exists FAIL` and the UI shows a volume message, this is why. Two ways past it:

1. **Use a second independent wallet instead.** A sub-account is a convenience, not a requirement — the hedge only needs a *second account* with its own net position. Any fresh address qualifies, with **no volume gate**, funded by an internal USDC send. `SubAccountAdapter` already takes an address and a signing key; a standalone wallet just skips the `vault_address` argument.
2. **Wait.** The bot generates volume by trading normally, so the gate clears on its own.

> A `volume_farm.py` tool that churned market orders to clear this gate has been
> removed. It worked, but the hazard was never the fees (~$44 per $100k at the
> base taker rate) — it was collision: Hyperliquid holds one net position per
> coin, so a reduce-only close could close *the bot's* position and desync its
> state. On a thin testnet book the slippage also dwarfed the fees.

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
| `STRATEGY` | `trend` | `trend` (EMA/ATR trend-following) or `hedge` (manual catalyst hedge only, no directional trading) |
| `TIMEFRAME` | `15m` | Primary candle timeframe (use `4h` for `STRATEGY=trend`) |
| `POLL_SECONDS` | `30` | Seconds between each polling loop |
| `LOOKBACK_CANDLES` | `200` | Number of candles fetched for indicator calculation |
| `HEARTBEAT_INTERVAL` | `5` | Print/log heartbeat every N loops |
| `LOG_SLIM_HEARTBEAT` | `true` | Omit `positions`/`statuses` from routine heartbeat lines (`logs/state.json` keeps the latest full snapshot) |
| `LOG_HEARTBEAT_VERBOSE_EVERY` | `20` | Keep one full heartbeat every N heartbeats (changes and risk blocks are always full) |
| `LOG_ERROR_MAX_CHARS` | `300` | Cap on logged error text; HTML gateway pages collapse to e.g. `502 Bad Gateway` |

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

### Strategy — Trend-following (`STRATEGY=trend`, best on `TIMEFRAME=4h`)

| Variable | Default | Description |
|----------|---------|-------------|
| `DIRECTION_MODE` | `static` | `static` = `SHORT_SYMBOLS` pins each symbol's side; `signal` = the trend picks it per bar |
| `TREND_EMA_PERIOD` | `200` | Regime EMA — price must be on its favorable side to enter |
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

### Execution

| Variable | Default | Description |
|----------|---------|-------------|
| `ENTRY_FEE_BPS` | `2` | Entry fee in basis points (limit/maker) |
| `EXIT_FEE_BPS` | `3.5` | Exit fee in basis points (market/taker) |
| `SLIPPAGE_BPS` | `2` | Simulated slippage on exits |

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
  bot.py            Main polling loop and per-symbol processing
  exchange.py       Hyperliquid official SDK adapter (with retry) + paper / live broker
  indicators.py     Shared ATR/EMA/ADX math + the opt-in entry gates
  strategy_trend.py STRATEGY=trend: EMA cross + regime, ATR stops, chandelier trail, risk sizing
  direction.py      Which side a symbol trades this bar (DIRECTION_MODE)
  hedge.py              Catalyst hedge: state machine + logs/hedge.json request channel
  hedge_broker.py       Catalyst hedge: sizing, leg execution, cut and trail
  subaccount.py         Sub-account order routing (vaultAddress), status and preflight
  risk.py           Timer-based halt logic (sizing lives in strategy_trend)
  config.py         Loads Settings from .env
  models.py         Shared types: Position, Leg, TradeResult
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
scripts/
  check-direction.py  Preflight: which side each symbol would trade right now
  smoke-check.py    Paper-mode startup check: config, one real tick, agent-tool allowlist
  log-stats.py      Summarize logs/events.jsonl volume by event type
  remote-update.sh  VM deploy script (CI/CD + manual)
  sync-logs.ps1     Pull live logs from GCP to ./logs (Windows)
  sync-logs.sh      Pull live logs from GCP to ./logs (bash)
  tunnel-dashboard.*  SSH tunnel to VM dashboard
data/
  *.json            Cached OHLCV candle data for backtesting
```

## Deployment

See [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md) for a step-by-step guide to run the bot 24/7 on a GCP VM with systemd auto-restart and log rotation.

## Warning

This is educational software, not financial advice. Futures trading with leverage can result in rapid losses. Always test in paper mode first and use small sizes when transitioning to live.
