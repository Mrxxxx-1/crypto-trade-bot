# OKX BTC/ETH Futures Bot (Paper-First)

Automated crypto futures bot for **BTC-USDT-SWAP** and **ETH-USDT-SWAP** on OKX.
Runs in paper mode by default -- live trading is intentionally disabled.

## Strategy

**EMA crossover + ATR volatility filter**, with optional higher-timeframe confirmation and trailing stops.

| Step | Detail |
|------|--------|
| Signal | Fast EMA crosses above slow EMA -> long; below -> short. Flat when ATR < `ATR_MIN_PCT`. |
| HTF filter | If `HTF_TIMEFRAME` is set, the signal must agree with the higher-timeframe EMA trend. |
| Volume filter | If `VOLUME_MIN_MULT` > 0, latest candle volume must exceed the rolling average by that factor. |
| Entry | Limit order at current price (+ maker/taker fee-edge tolerance) for immediate fill. |
| Stop / TP | ATR-based. Stop = `STOP_ATR_MULTIPLIER` x ATR away; TP = stop distance x `TAKE_PROFIT_R`. ATR source is configurable (`STOP_ATR_SOURCE`). |
| Trailing stop | When enabled (`TRAIL_AFTER_R` > 0), the stop ratchets toward price after reaching N x R in profit. Trail distance = `TRAIL_ATR_MULTIPLIER` x ATR. |
| Exit trigger | Current candle high/low checked against stop/TP each poll. Skipped on the entry candle. |
| Signals | Generated from **closed candles only** (the forming candle is excluded). |

## Risk Engine

| Guard | Trigger | Action |
|-------|---------|--------|
| Per-trade sizing | `RISK_PER_TRADE_PCT` of equity / per-unit risk | Caps position size |
| Max leverage | Notional > `MAX_LEVERAGE` x equity | Caps position size |
| Consecutive losses | Streak >= `MAX_CONSECUTIVE_LOSSES` | Halt all trading for `CONSEC_HALT_HOURS` |
| Rolling drawdown | Drawdown from window start >= `MAX_DAILY_LOSS_PCT` | Halt all trading for `DAILY_LOSS_HALT_HOURS` |
| Cooldown | After a stop exit | Block same-direction re-entry for `COOLDOWN_CANDLES` x timeframe minutes |

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

### 3. Fetch historical data (for backtesting)

```powershell
python -m src.fetch_candles --days 45
```

This downloads OHLCV candles to `data/` for both the primary and HTF timeframes.

### 4. Backtest

```powershell
python -m src.backtest
python -m src.backtest --set TAKE_PROFIT_R=1.5 STOP_ATR_MULTIPLIER=2.0
python -m src.backtest --cooldown 12 --label TIGHT_CD
```

### 5. Run the bot

```powershell
python -m src.main
```

The bot polls OKX every `POLL_SECONDS`, generates signals, simulates orders in paper mode, and writes logs to `logs/events.jsonl` and `logs/trades.jsonl`.

## Environment Variables

All variables are loaded from `.env` via `python-dotenv`. See `.env.example` for a complete template.

### Connection

| Variable | Default | Description |
|----------|---------|-------------|
| `MODE` | `paper` | `paper` or `live` (live is blocked in this version) |
| `OKX_API_KEY` | | OKX API key |
| `OKX_API_SECRET` | | OKX API secret |
| `OKX_API_PASSPHRASE` | | OKX API passphrase |

### Market

| Variable | Default | Description |
|----------|---------|-------------|
| `SYMBOLS` | `BTC-USDT-SWAP,ETH-USDT-SWAP` | Comma-separated perpetual swap symbols |
| `TIMEFRAME` | `5m` | Primary candle timeframe |
| `POLL_SECONDS` | `20` | Seconds between each polling loop |
| `LOOKBACK_CANDLES` | `200` | Number of candles fetched for indicator calculation |
| `HEARTBEAT_INTERVAL` | `1` | Print/log heartbeat every N loops |

### Position Sizing

| Variable | Default | Description |
|----------|---------|-------------|
| `INITIAL_EQUITY` | `10000` | Starting paper equity (USD) |
| `MAX_LEVERAGE` | `3` | Max notional / equity ratio |
| `RISK_PER_TRADE_PCT` | `0.5` | Equity % risked per trade |

### Risk Guards

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_DAILY_LOSS_PCT` | `2.0` | Rolling drawdown % that triggers a halt |
| `MAX_CONSECUTIVE_LOSSES` | `3` | Loss streak that triggers a halt |
| `CONSEC_HALT_HOURS` | `6` | Hours to pause after consecutive-loss halt |
| `DAILY_LOSS_HALT_HOURS` | `12` | Hours to pause after drawdown halt |
| `COOLDOWN_CANDLES` | `0` | Candles to wait after stop exit before same-direction re-entry |

### Strategy

| Variable | Default | Description |
|----------|---------|-------------|
| `FAST_EMA` | `20` | Fast EMA period |
| `SLOW_EMA` | `50` | Slow EMA period |
| `ATR_PERIOD` | `14` | ATR lookback |
| `ATR_MIN_PCT` | `0.2` | Minimum ATR as % of price to generate a signal |
| `STOP_ATR_MULTIPLIER` | `1.8` | Stop distance = ATR x this |
| `TAKE_PROFIT_R` | `1.5` | TP distance = stop distance x this |
| `HTF_TIMEFRAME` | `1h` | Higher-timeframe for trend confirmation (empty to disable) |
| `STOP_ATR_SOURCE` | `primary` | `primary` or `htf` -- which timeframe's ATR to use for stop/TP |
| `VOLUME_MIN_MULT` | `0.0` | Min volume as multiple of rolling average (0 = disabled) |

### Trailing Stop

| Variable | Default | Description |
|----------|---------|-------------|
| `TRAIL_AFTER_R` | `0.0` | Activate trailing stop after this many R in profit (0 = disabled) |
| `TRAIL_ATR_MULTIPLIER` | `2.0` | Trail distance = ATR x this |

### Execution

| Variable | Default | Description |
|----------|---------|-------------|
| `ENTRY_FEE_BPS` | `2` | Entry fee in basis points (limit/maker) |
| `EXIT_FEE_BPS` | `5` | Exit fee in basis points (market/taker) |
| `SLIPPAGE_BPS` | `2` | Simulated slippage on exits |
| `LIMIT_TIMEOUT_SECONDS` | `30` | Cancel unfilled limit orders after this many seconds |

## Project Layout

```
src/
  main.py           Entry point -- blocks live mode, runs paper bot
  bot.py            Main polling loop and per-symbol processing
  exchange.py       OKX ccxt adapter (with retry) + paper broker
  strategy.py       EMA crossover / ATR / volume / HTF signals
  risk.py           Position sizing and timer-based halt logic
  config.py         Loads Settings from .env
  models.py         Shared types: Position, TradeResult, PendingOrder
  backtest.py       Offline backtester on cached candle data
  fetch_candles.py  Download & cache OHLCV from OKX
logs/
  events.jsonl      Heartbeats, position opens/closes, errors
  trades.jsonl      Completed trade records with P&L
data/
  *.json            Cached OHLCV candle data for backtesting
```

## Deployment

See [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md) for a step-by-step guide to run the bot 24/7 on a GCP VM with systemd auto-restart and log rotation.

## Warning

This is educational software, not financial advice. Futures trading with leverage can result in rapid losses. Always test in paper mode first and use small sizes when transitioning to live.
