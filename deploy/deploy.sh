#!/bin/bash
# Pull latest code and restart the bot.
# Run as root or botuser from any directory.
# Usage: bash deploy/deploy.sh
set -e

REPO_DIR=/home/botuser/scammer-list-bot

echo "⬇  Pulling latest code …"
sudo -u botuser git -C "$REPO_DIR" pull --ff-only

echo "📦 Updating dependencies …"
sudo -u botuser "$REPO_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u botuser "$REPO_DIR/venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"

echo "🔄 Restarting service …"
systemctl restart scammer-bot
sleep 2
systemctl status scammer-bot --no-pager

echo "✅ Deploy done. Logs: journalctl -u scammer-bot -f"
