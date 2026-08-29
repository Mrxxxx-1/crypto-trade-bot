# GCP Deployment Guide

Deploy the Hyperliquid futures bot on a **non-US** GCP VM for 24/7 operation.
Hyperliquid blocks US IP addresses, so the VM **must** be in a non-US region.

## 1) Create VM on GCP

- Open GCP Console -> Compute Engine -> VM instances -> Create instance.
- Suggested settings:
  - Name: `crypto-bot`
  - **Region: non-US** — recommended: `europe-west1` (Belgium), `europe-west4` (Netherlands), `asia-southeast1` (Singapore), `asia-east1` (Taiwan)
  - Machine type: `e2-small` (2 vCPU, 2 GB — sufficient for the bot)
  - OS: Ubuntu 22.04 LTS
  - Disk: 20 GB
  - Firewall: allow SSH (`tcp:22`)

Optional:

- Reserve a static external IP for easier reconnect.

> **Why non-US?** Hyperliquid geo-blocks US IPs for both public and trading APIs.
> Verify with: `curl -s https://api.hyperliquid.xyz/info -d '{"type":"meta"}'` from the VM — a JSON response means the API is reachable.

## 2) Connect and prepare server

SSH into the VM, then run:

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install git python3-venv python3-pip
```

Optional security hardening:

```bash
sudo ufw allow OpenSSH
sudo ufw enable
```

## 3) Upload project to VM

Use one of these:

- `git clone https://github.com/Mrxxxx-1/crypto-trade-bot.git`
- or upload the local folder with `scp`/`rsync`

Then enter the project directory:

```bash
cd crypto-trade-bot
```

## 4) Create Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 5) Configure

```bash
cp .env.example .env
nano .env          # or vim / your editor
mkdir -p logs
```

### Paper mode (recommended first)

Set in `.env`:

- `MODE=paper`
- Leave `HL_WALLET_ADDRESS` and `HL_PRIVATE_KEY` empty (public data only)
- Review strategy / risk parameters

### Live mode

Set in `.env`:

- `MODE=live`
- `HL_TESTNET=true` to trade on Hyperliquid **testnet** first (fake funds), `false` for mainnet
- Review `MAX_LEVERAGE`, `RISK_PER_TRADE_PCT` — these directly affect real money

#### Credentials: direct wallet vs API (agent) wallet

The bot signs with `HL_PRIVATE_KEY` and queries balances/positions under
`HL_WALLET_ADDRESS`. There are two valid setups:

- **Direct wallet** — `HL_PRIVATE_KEY` is your wallet's key and
  `HL_WALLET_ADDRESS` is that same wallet's address.
- **API / agent wallet (recommended)** — generate an API wallet in the
  Hyperliquid UI (it can trade but **cannot withdraw**). Then:
  - `HL_PRIVATE_KEY` = the **API wallet's** private key
  - `HL_WALLET_ADDRESS` = your **main account** address (the funded one)

  The adapter detects that the key's address differs from `HL_WALLET_ADDRESS`
  and signs as the agent on behalf of the main account. Funds and positions
  are read from the main account.

#### Funding (unified account)

`fetch_balance()` returns your **total USDC equity = perps account value + spot
USDC**, so a **unified account** (USDC held in spot, used as perp collateral) is
fully supported — you do **not** need to manually move USDC into the perps
wallet. If your account is *not* unified and USDC sits unusable in spot, transfer
it to perps (UI, or the SDK's `usd_class_transfer`) or the bot will size to 0.

#### Strategy & direction

- `STRATEGY=trend` (EMA/ATR trend-follower, recommended on `TIMEFRAME=4h`) or `dca`
- `SHORT_SYMBOLS=` controls direction per symbol: bases listed here trade
  **short-only** (mirror logic), everything else is **long-only**. A long-only
  bot stays flat in a downtrend; add symbols here to trade the short side.
- Opt-in entry filters (off by default): `ADX_MIN` (chop filter, trend),
  `VOLUME_MIN_MULT` (volume confirmation), `MTF_ENABLED` (higher-timeframe
  alignment), `DCA_CHANDELIER_ENABLED`. See the README "Entry-quality filters"
  table for details.

> **Startup reconciliation:** on launch the live bot adopts any open positions
> already on the account and manages them with the active strategy — including
> **closing** a position that no longer fits (e.g. a long when the trend has
> flipped). Don't hand-trade the same account while the bot runs.

**Security:**

```bash
chmod 600 .env
```

This restricts `.env` to the file owner — critical because it contains your private key.

### Optional: daily Telegram briefing

Also in `.env`:

- `DAILY_BRIEFING_ENABLED=true`
- `TELEGRAM_BOT_TOKEN=…`
- `TELEGRAM_CHAT_ID=…`
- `GEMINI_API_KEY=…`

## 6) Verify API access from the VM

Before running the bot, confirm the VM can reach Hyperliquid:

```bash
source .venv/bin/activate
# Mainnet:
python -c "from hyperliquid.info import Info; from hyperliquid.utils import constants as c; i=Info(c.MAINNET_API_URL, skip_ws=True); print('BTC mid:', i.all_mids()['BTC'])"
# Testnet (if HL_TESTNET=true):
python -c "from hyperliquid.info import Info; from hyperliquid.utils import constants as c; i=Info(c.TESTNET_API_URL, skip_ws=True); print('BTC mid:', i.all_mids()['BTC'])"
```

If this prints a price, the VM region is not geo-blocked. If it errors with a connection/403, try a different non-US region.

**Credentials preflight (live mode):** confirm the bot resolves your account and
sees your balance before starting the service — this places **no orders**:

```bash
python -c "from src.config import load_settings; from src.exchange import ExchangeAdapter; s=load_settings(); a=ExchangeAdapter(s); print('mode',s.mode,'testnet',s.testnet); print('account',s.wallet_address); print('balance',a.fetch_balance()); print('positions',a.fetch_positions())"
```

A non-zero `balance` (your total unified USDC) means credentials and funding are
correct. `0.0` means wrong account address or an unfunded account.

## 7) Smoke test manually

Run:

```bash
python -m src.main
```

Expected:

- Startup line showing mode (paper or live), symbols, **strategy + timeframe**, and the active filters
- In live mode, a `[reconcile] adopted open …` line for any position already on the account (or nothing if flat)
- Periodic heartbeat lines
- `logs/events.jsonl` keeps growing

Stop with `Ctrl+C`.

## 8) Configure systemd auto-restart service

Get your username:

```bash
whoami
```

Create service file:

```bash
sudo tee /etc/systemd/system/crypto-bot.service > /dev/null <<'EOF'
[Unit]
Description=Crypto Trade Bot (Hyperliquid)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mrx10210
WorkingDirectory=/home/mrx10210/crypto-trade-bot
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/mrx10210/crypto-trade-bot/.venv/bin/python -m src.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

Replace `mrx10210` in the file:

```bash
sudo sed -i "s/mrx10210/$(whoami)/g" /etc/systemd/system/crypto-bot.service
```

Enable and start service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable crypto-bot
sudo systemctl start crypto-bot
```

Check status:

```bash
systemctl status crypto-bot
journalctl -u crypto-bot -f
```

## 9) Log and health checks

Daily checks:

- service is running: `systemctl status crypto-bot`
- heartbeat is recent in journal/logs
- `logs/events.jsonl` and `logs/trades.jsonl` are updating
- disk usage healthy: `df -h`
- if daily briefing is enabled, check `logs/briefings.jsonl` for the latest entry

## 10) Basic log rotation (recommended)

Create logrotate config:

```bash
sudo tee /etc/logrotate.d/crypto-bot > /dev/null <<'EOF'
/home/*/crypto-trade-bot/logs/*.jsonl {
    daily
    rotate 14
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
}
EOF
```

## 10b) Optional services: web dashboard + Telegram control

These run as **separate systemd services** alongside `crypto-bot`, sharing the
same `logs/` directory. Neither can place orders — the agent layer can only
read stats and pause/resume.

### Web dashboard (read-only monitoring UI)

#### Option A — Public demo with **fake data** (recommended for reviewers)

Use a **separate small VM** (or the same VM without running the real bot) so
you can share a URL with anyone without exposing real trades or API keys.

In `.env` on that VM:

```bash
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8000
DASHBOARD_DEMO_MODE=true
```

Do **not** copy your real `.env` with Telegram/Gemini/wallet secrets to this VM.
`demo_logs/` in the repo supplies all dashboard data; the bot service is optional.

Open GCP firewall `tcp:8000` to `0.0.0.0/0` (or your region), then install the
dashboard service below. Visitors see a **DEMO** banner and sample paper stats.

#### Option B — Live dashboard (your real bot logs)

Open the dashboard port in the GCP firewall first (VPC network → Firewall →
allow `tcp:8000` from your IP), keep `DASHBOARD_DEMO_MODE=false`, then:

```bash
sudo tee /etc/systemd/system/crypto-dashboard.service > /dev/null <<'EOF'
[Unit]
Description=Crypto Bot Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mrx10210
WorkingDirectory=/home/mrx10210/crypto-trade-bot
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/mrx10210/crypto-trade-bot/.venv/bin/python -m src.webapp
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo sed -i "s/mrx10210/$(whoami)/g" /etc/systemd/system/crypto-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-dashboard
```

Then browse to `http://35.200.32.57:8000`. To avoid exposing it publicly,
set `DASHBOARD_HOST=127.0.0.1` in `.env` and reach it via an SSH tunnel
(`ssh -L 8000:localhost:8000 user@vm`) or put nginx + TLS in front.

### Two-way Telegram control

Requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` (Gemini optional,
for natural-language commands). No inbound port needed — it uses long polling.

```bash
sudo tee /etc/systemd/system/crypto-telegram.service > /dev/null <<'EOF'
[Unit]
Description=Crypto Bot Telegram Control
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mrx10210
WorkingDirectory=/home/mrx10210/crypto-trade-bot
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/mrx10210/crypto-trade-bot/.venv/bin/python -m src.telegram_control
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo sed -i "s/mrx10210/$(whoami)/g" /etc/systemd/system/crypto-telegram.service
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-telegram
```

> Alternatively, set `TELEGRAM_CONTROL_ENABLED=true` in `.env` to run the
> listener inside the main `crypto-bot` process and skip this service.

Message your bot `/help` to confirm it responds. Only messages from your
`TELEGRAM_CHAT_ID` are honored.

### MCP server

The MCP server (`python -m src.mcp_server`) is launched on demand by an MCP
client (Cursor / Claude Desktop) over stdio — it is **not** a long-running
systemd service. See the README for an example client config.

## 11) Paper → Live transition checklist

Before switching `MODE=live`:

1. (Optional) Validate on **testnet** first: `HL_TESTNET=true` with an API wallet
   and faucet USDC — same code path as mainnet, zero financial risk.
2. Run paper mode for at least 1 week — verify:
   - no crashes or restart loops
   - open/close events appear correctly in logs
   - risk halts fire as expected
   - drawdown and consistency are within your plan
3. Fund your Hyperliquid wallet with USDC (deposit via Arbitrum bridge). A
   **unified account** can hold the USDC in spot — the bot reads the combined
   balance. Confirm with the credentials preflight in step 6.
4. `RISK_PER_TRADE_PCT` sizes off live equity, so `INITIAL_EQUITY` is only the
   paper-mode starting balance; live equity is read from the exchange.
5. Start with **conservative** settings: low `RISK_PER_TRADE_PCT` (e.g. 0.3), low
   `MAX_LEVERAGE` (e.g. 2). Consider enabling `ADX_MIN=25` to skip choppy entries.
6. Update `.env`: `MODE=live`, set `HL_WALLET_ADDRESS` (main account) +
   `HL_PRIVATE_KEY` (wallet or API-wallet key), and confirm direction via
   `SHORT_SYMBOLS`.
7. `chmod 600 .env` (restrict access)
8. Restart: `sudo systemctl restart crypto-bot`
9. Monitor the first few trades closely via `journalctl -u crypto-bot -f` and Telegram briefing

## 12) Clean re-clone + start newest version

Use this when you want a fully fresh deployment from the latest repo.

1) Stop the running bot service:

```bash
sudo systemctl stop crypto-bot || true
sudo systemctl disable crypto-bot || true
```

2) Backup current config and logs:

```bash
cd ~
cp ~/crypto-trade-bot/.env ~/bot-env-backup-$(date +%F-%H%M).env 2>/dev/null || true
mkdir -p ~/bot-log-backup
cp -r ~/crypto-trade-bot/logs ~/bot-log-backup/logs-$(date +%F-%H%M) 2>/dev/null || true
```

3) Delete old repo and clone latest:

```bash
rm -rf ~/crypto-trade-bot
git clone https://github.com/Mrxxxx-1/crypto-trade-bot.git ~/crypto-trade-bot
cd ~/crypto-trade-bot
```

4) Recreate venv and install requirements:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

5) Restore `.env` (or use paper defaults) and run smoke test:

```bash
cp ~/bot-env-backup-*.env .env 2>/dev/null || cp .env.example .env
chmod 600 .env
mkdir -p logs
python -m src.main
```

Wait for startup/heartbeat output, then stop with `Ctrl+C`.

6) Start service again:

```bash
sudo systemctl daemon-reload
sudo systemctl enable crypto-bot
sudo systemctl start crypto-bot
systemctl status crypto-bot
journalctl -u crypto-bot -f
```

Quick update-only path (no delete/re-clone):

```bash
cd ~/crypto-trade-bot
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart crypto-bot crypto-telegram crypto-dashboard
```

Or use CI/CD (section 13): push to `main` and GitHub Actions runs the same steps via `scripts/remote-update.sh`.

## 13) CI/CD (GitHub Actions → GCP)

Push to `main` runs **CI** (compile + import smoke test), then **deploys** to your VM
over SSH. Pull requests only run CI.

### One-time setup

**On the VM** (if not already done): clone the repo, create `.env`, enable
`crypto-bot` systemd — see steps 3–8 above. The deploy workflow does **not**
create or overwrite `.env`.

**SSH key for GitHub Actions → VM:**

On your laptop (or any secure machine):

```bash
ssh-keygen -t ed25519 -f gcp_actions_deploy -N ""
```

On the **VM**, append the **public** key to `~/.ssh/authorized_keys`:

```bash
# paste contents of gcp_actions_deploy.pub
nano ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

In **GitHub → Settings → Secrets**, set `GCP_SSH_PRIVATE_KEY` to the full contents
of `gcp_actions_deploy` (the private key file).

**Private repo:** the VM also needs git read access — add a separate
[deploy key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)
(read-only) to the repo and configure `~/.ssh` on the VM for `git fetch`.
Public repos need no extra git auth.

**GitHub repository secrets** (Settings → Secrets and variables → Actions):

| Secret | Example | Purpose |
|--------|---------|---------|
| `GCP_SSH_HOST` | `35.200.32.57` | VM external IP |
| `GCP_SSH_USER` | `mrx10210` | Linux username on the VM |
| `GCP_SSH_PRIVATE_KEY` | `-----BEGIN OPENSSH PRIVATE KEY-----…` | Private key that can SSH as `GCP_SSH_USER` |
| `GCP_DEPLOY_PATH` | `/home/mrx10210/crypto-trade-bot` | Optional; full path on VM (recommended over `~`) |

Optional: create a **production** environment in GitHub (Settings → Environments)
to require manual approval before deploy.

**Workflow file:** `.github/workflows/ci-cd.yml`  
**Remote script:** `scripts/remote-update.sh` (git pull, pip install, restart services)

Manual deploy from your laptop (same as CI):

```bash
ssh user@VM_IP "bash -s -- /home/user/crypto-trade-bot" < scripts/remote-update.sh
```

Or trigger **Actions → CI/CD → Run workflow** in GitHub.

### What deploy does

1. `git fetch origin main && git reset --hard origin/main`
2. Recreate/refreshes `.venv` and `pip install -r requirements.txt`
3. Restarts `crypto-bot`, and `crypto-dashboard` / `crypto-telegram` if enabled

`.env` and `logs/` on the VM are never touched by CI/CD.

---

## 14) Access live logs from your local machine

Two options while the bot runs on GCP:

### A) Sync log files locally (events, trades, briefings)

1. Copy `scripts/deploy.local.example` → `scripts/.deploy.local` and fill in
   `GCP_SSH_HOST`, `GCP_SSH_USER`, `GCP_DEPLOY_PATH`.
2. Pull logs:

**Windows (PowerShell):**

```powershell
.\scripts\sync-logs.ps1
.\scripts\sync-logs.ps1 -Tail    # sync + show last 5 lines
```

**Linux / macOS:**

```bash
bash scripts/sync-logs.sh
bash scripts/sync-logs.sh --tail
```

Files land in `./logs/` — same paths the bot and MCP tools use locally
(`events.jsonl`, `trades.jsonl`, `briefings.jsonl`, `control.json`).

Use this to inspect live paper/live runs with your editor, run `python -m src.briefing`
against real data, or point MCP at synced logs.

### B) Dashboard over SSH tunnel (live UI, no public port)

On the VM, run the dashboard bound to localhost only:

```bash
# in .env on the VM
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8000
DASHBOARD_DEMO_MODE=false
```

Enable `crypto-dashboard` systemd (section 10b), then from your laptop:

```powershell
.\scripts\tunnel-dashboard.ps1
# open http://127.0.0.1:8000
```

```bash
bash scripts/tunnel-dashboard.sh
```

The dashboard reads the VM's `logs/` in real time; nothing is exposed on the
public internet.

---

## Troubleshooting

- **API connection refused / 403:** the VM is in a geo-blocked region. Recreate in a non-US region (see step 1).
- **No output in terminal:** check heartbeat setting and `POLL_SECONDS`.
- **Service not starting:** `journalctl -u crypto-bot -n 100`.
- **Missing Python packages:** activate venv and rerun `pip install -r requirements.txt`.
- **Bot appears idle:** if signals stay `flat`, this can be normal; adjust test profile when validating open/close flow.
- **Live order errors:** check `logs/events.jsonl` for `order_error` or `close_error` events. Verify wallet has USDC balance and private key is correct.
- **Bot won't open positions / `balance=0.0`:** run the credentials preflight (step 6). Either `HL_WALLET_ADDRESS` is the wrong account (must be the **main** account, not the API-wallet address) or the account is unfunded. With a non-unified account, move USDC from spot into perps.
- **Bot closed a position I opened by hand:** expected — on startup the live bot reconciles and manages every open position, and closes any that don't fit the active strategy (look for `position_reconciled` then a `regime`/`trail` exit). Don't hand-trade the bot's account.
- **Long-only bot does nothing in a downtrend:** also expected. Add the symbol's base to `SHORT_SYMBOLS` to trade the short side, or wait for an uptrend.
- **Trend entries skipped with `adx_chop`:** the `ADX_MIN` filter is suppressing weak-trend entries; lower `ADX_MIN` (or set 0) if you want more trades.
