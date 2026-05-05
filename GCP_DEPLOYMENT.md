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
- `HL_WALLET_ADDRESS=0x…` — your EVM wallet address
- `HL_PRIVATE_KEY=…` — hex private key for that wallet (**keep this file secure**)
- `HL_TESTNET=false` (set `true` to test on Hyperliquid testnet first)
- Review `MAX_LEVERAGE`, `RISK_PER_TRADE_PCT`, `INITIAL_EQUITY` — these directly affect real money

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
python -c "from hyperliquid.info import Info; from hyperliquid.utils import constants as c; i=Info(c.MAINNET_API_URL, skip_ws=True); print('BTC mid:', i.all_mids()['BTC'])"
```

If this prints a price, the VM region is not geo-blocked. If it errors with a connection/403, try a different non-US region.

## 7) Smoke test manually

Run:

```bash
python -m src.main
```

Expected:

- Startup line showing mode (paper or live), symbols, leverage cap
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

## 11) Paper → Live transition checklist

Before switching `MODE=live`:

1. Run paper mode for at least 1 week — verify:
   - no crashes or restart loops
   - open/close events appear correctly in logs
   - risk halts fire as expected
   - drawdown and consistency are within your plan
2. Fund your Hyperliquid wallet with USDC (deposit via Arbitrum bridge)
3. Set `INITIAL_EQUITY` to match your actual deposited balance
4. Start with **conservative** settings: low `RISK_PER_TRADE_PCT` (e.g. 0.3), low `MAX_LEVERAGE` (e.g. 2)
5. Update `.env`: `MODE=live`, add `HL_WALLET_ADDRESS` + `HL_PRIVATE_KEY`
6. `chmod 600 .env` (restrict access)
7. Restart: `sudo systemctl restart crypto-bot`
8. Monitor the first few trades closely via `journalctl -u crypto-bot -f` and Telegram briefing

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
sudo systemctl restart crypto-bot
```

## Troubleshooting

- **API connection refused / 403:** the VM is in a geo-blocked region. Recreate in a non-US region (see step 1).
- **No output in terminal:** check heartbeat setting and `POLL_SECONDS`.
- **Service not starting:** `journalctl -u crypto-bot -n 100`.
- **Missing Python packages:** activate venv and rerun `pip install -r requirements.txt`.
- **Bot appears idle:** if signals stay `flat`, this can be normal; adjust test profile when validating open/close flow.
- **Live order errors:** check `logs/events.jsonl` for `order_error` or `close_error` events. Verify wallet has USDC balance and private key is correct.
