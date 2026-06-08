#!/bin/bash
# Run once on a fresh Hostinger VPS (Ubuntu 22.04/24.04)
# Usage: bash deploy/setup.sh
set -e

# ── 1. System deps ────────────────────────────────────────────────────────────
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git

# ── 2. Create a dedicated non-root user ───────────────────────────────────────
if ! id -u botuser &>/dev/null; then
    useradd -m -s /bin/bash botuser
fi

# ── 3. Clone repo ─────────────────────────────────────────────────────────────
REPO_DIR=/home/botuser/scammer-list-bot

if [ ! -d "$REPO_DIR" ]; then
    sudo -u botuser git clone https://github.com/rishi866/scammer-list-bot.git "$REPO_DIR"
else
    echo "Repo already cloned — skipping clone step."
fi

# ── 4. Python venv + deps ─────────────────────────────────────────────────────
sudo -u botuser python3 -m venv "$REPO_DIR/venv"
sudo -u botuser "$REPO_DIR/venv/bin/pip" install --upgrade pip
sudo -u botuser "$REPO_DIR/venv/bin/pip" install -r "$REPO_DIR/requirements.txt"

# ── 5. .env file ──────────────────────────────────────────────────────────────
if [ ! -f "$REPO_DIR/.env" ]; then
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
    echo ""
    echo ">>> Edit $REPO_DIR/.env with your BOT_TOKEN, ADMIN_IDS, and DATABASE_URL"
    echo "    then run:  systemctl start scammer-bot"
fi

# ── 6. systemd service ────────────────────────────────────────────────────────
cp "$REPO_DIR/deploy/scammer-bot.service" /etc/systemd/system/scammer-bot.service
systemctl daemon-reload
systemctl enable scammer-bot

echo ""
echo "✅ Setup complete."
echo "   Fill in .env, then:  systemctl start scammer-bot"
echo "   Logs:                journalctl -u scammer-bot -f"
