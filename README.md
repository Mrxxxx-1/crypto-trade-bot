# OKX BTC/ETH Futures Bot (Paper-First)

A customized crypto futures bot focused on:

- `BTC-USDT-SWAP`
- `ETH-USDT-SWAP`
- Risk-first execution with paper trading by default

This project starts safely at **3x target leverage exposure** in simulation and is designed to ramp toward 5x-10x only after passing clear risk gates.

## What This MVP Includes

- OKX market data via `ccxt`
- Simple trend strategy (EMA crossover + ATR volatility filter)
- Built-in risk engine:
  - per-trade equity risk cap
  - max leverage cap
  - max daily loss cutoff
  - max consecutive losses cooldown
- Paper exchange simulator (fake money first)
- Live mode intentionally disabled in this MVP (paper-first safety)
- JSONL trade/event logs for analysis

## Important Warning

This is educational software, not financial advice. Live trading futures can lose money quickly, especially with leverage. Test in demo/paper first and use small sizes.

## Quick Start

### 1) Create and activate Python environment

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2) Configure environment

Copy `.env.example` to `.env` and edit values.

```powershell
copy .env.example .env
```

Set these first:

- `MODE=paper`
- `INITIAL_EQUITY=10000`
- `TARGET_LEVERAGE=3`
- `MAX_LEVERAGE=3`

### 3) Run the bot

```powershell
python -m src.main
```

The bot will:

- pull OHLCV/ticker from OKX
- generate signals for BTC/ETH
- simulate orders in paper mode
- write logs to `logs/events.jsonl` and `logs/trades.jsonl`

## Default Risk Profile (Starter)

- Per-trade risk: `0.5%` of equity
- Daily loss limit: `2%`
- Max consecutive losses: `3`
- Target leverage: `3x`

You can tune these in `.env`.

## Suggested 3x -> 5x Progression

1. Run paper continuously for at least 2 weeks.
2. Validate:
   - no order/state desync
   - no risk rule bypass
   - positive expectancy after costs
3. Move to tiny live size at 3x.
4. Increase to 5x only when drawdown and execution remain stable.

## Environment Variables

See `.env.example` for the full list.

Key vars:

- `MODE`: keep `paper` for now
- `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE`
- `SYMBOLS`: comma-separated list
- `TIMEFRAME`: default `5m`
- `POLL_SECONDS`: default `20`
- `HEARTBEAT_INTERVAL`: print/log status every N loops (default `1`)
- `RISK_PER_TRADE_PCT`
- `MAX_DAILY_LOSS_PCT`
- `MAX_CONSECUTIVE_LOSSES`
- `TARGET_LEVERAGE`, `MAX_LEVERAGE`

## Project Layout

- `src/main.py` - entrypoint
- `src/bot.py` - main run loop
- `src/exchange.py` - OKX + paper execution wrapper
- `src/strategy.py` - signal generation
- `src/risk.py` - risk sizing and kill-switch logic
- `src/config.py` - env config

## Next Steps

- Add a full backtester with historical data.
- Add websocket market stream and order-state reconciliation.
- Add Prometheus/Grafana or Telegram alerts.
- Add separate configs for paper vs live profiles.
