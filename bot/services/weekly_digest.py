"""Weekly digest — sends stats to all admins every 7 days."""
from __future__ import annotations

import asyncio
import logging
import os

from telegram import Bot
from telegram.error import TelegramError

from bot.db import get_weekly_stats
from bot.services.admins import get_admin_ids as _admin_ids
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)

DIGEST_INTERVAL = 7 * 24 * 3600  # 7 days


async def send_digest(bot: Bot) -> None:
    stats = await get_weekly_stats()

    top_lines = []
    for i, r in enumerate(stats["top_reporters"], 1):
        name = f"@{r['reporter_username']}" if r.get("reporter_username") else str(r["reporter_id"])
        top_lines.append(f"  {i}. {name} — {r['cnt']} report(s)")

    top_str = "\n".join(top_lines) if top_lines else "  No reports this week."

    text = em(
        f"📊 <b>Weekly Digest</b>\n\n"
        f"🔴 Total scammers listed : <b>{stats['total_scammers']}</b>\n"
        f"📈 New this week         : <b>{stats['new_this_week']}</b>\n\n"
        f"📨 Reports this week     : <b>{stats['reports_week']}</b>\n"
        f"✅ Approved this week    : <b>{stats['approved_week']}</b>\n\n"
        f"🏆 <b>Top Reporters (7 days)</b>\n{top_str}"
    )

    for aid in _admin_ids():
        try:
            await bot.send_message(aid, text, parse_mode="HTML")
        except TelegramError as e:
            logger.warning("Could not send digest to admin %s: %s", aid, e)


async def weekly_digest_loop(bot: Bot) -> None:
    logger.info("Weekly digest loop started (every 7 days)")
    while True:
        try:
            await asyncio.sleep(DIGEST_INTERVAL)
            await send_digest(bot)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Weekly digest error: %s", e)
