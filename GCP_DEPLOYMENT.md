# GCP Deployment Guide (Paper Mode)

This guide deploys the bot to Google Cloud Platform for 24/7 paper trading.

## 1) Create VM on GCP

- Open GCP Console -> Compute Engine -> VM instances -> Create instance.
- Suggested settings:
  - Name: `crypto-bot-paper`
  - Region: nearest to your location/exchange region
  - Machine type: `e2-small` (good starter)
  - OS: Ubuntu 22.04 LTS
  - Disk: 20 GB
  - Firewall: allow SSH (`tcp:22`)

Optional:

- Reserve a static external IP for easier reconnect.

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

## 5) Configure paper profile

```bash
cp .env.production-paper .env
mkdir -p logs
```

Verify `.env` contains:

- `MODE=paper`
- `HEARTBEAT_INTERVAL` set as desired (`1` verbose, `3` quieter)
- `CONSEC_HALT_HOURS=6` and `DAILY_LOSS_HALT_HOURS=12` (safe defaults)

## 6) Smoke test manually

Run:

```bash
python -m src.main
```

Expected:

- startup line in terminal
- periodic heartbeat lines
- `logs/events.jsonl` keeps growing

Stop with `Ctrl+C`.

## 7) Configure systemd auto-restart service

Get your username:

```bash
whoami
```

Create service file:

```bash
sudo tee /etc/systemd/system/crypto-bot.service > /dev/null <<'EOF'
[Unit]
Description=Crypto Trade Bot (Paper)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/crypto-trade-bot
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/YOUR_USER/crypto-trade-bot/.venv/bin/python -m src.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

Replace `YOUR_USER` in the file:

```bash
sudo sed -i "s/YOUR_USER/$(whoami)/g" /etc/systemd/system/crypto-bot.service
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

## 8) Log and health checks

Daily checks:

- service is running: `systemctl status crypto-bot`
- heartbeat is recent in journal/logs
- `logs/events.jsonl` and `logs/trades.jsonl` are updating
- disk usage healthy: `df -h`

## 9) Basic log rotation (recommended)

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

## 10) Paper-run acceptance gate

Keep paper mode for 1-2 weeks before any live work.

Pass criteria:

- no recurring crashes/restart loops
- open/close events appear correctly
- risk guardrails fire as expected: timer halts (consecutive-loss / daily-loss), candle cooldown after stop exits, closed-candle signals, high/low exit checks
- drawdown and consistency are within your plan

## Troubleshooting

- **No output in terminal:** check heartbeat setting and `POLL_SECONDS`.
- **Service not starting:** `journalctl -u crypto-bot -n 100`.
- **Missing Python packages:** activate venv and rerun `pip install -r requirements.txt`.
- **Bot appears idle:** if signals stay `flat`, this can be normal; adjust test profile when validating open/close flow.
