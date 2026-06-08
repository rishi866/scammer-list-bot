"""Auto-update scammer usernames via Telegram's getChat API.

How it works
------------
Telegram IDs never change, but usernames can.  When a scammer changes
their username after a scam, a lookup by @username fails.  By storing the
ID and periodically calling bot.get_chat(id) we always have the latest
username — and keep the full history of old ones.

Limitations
-----------
bot.get_chat(user_id) only works when Telegram has the user in its cache
for our bot, i.e. the user has messaged the bot, or is in a group/channel
the bot is in.  If Telegram returns "Chat not found" we skip silently and
retry on the next cycle.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telegram import Bot
from telegram.error import TelegramError

from bot.db import (
    update_scammer_username,
    touch_username_check,
    get_scammers_needing_refresh,
)

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_HOURS = 6   # how often the background loop runs
BATCH_DELAY_SECONDS    = 1   # pause between individual getChat calls (rate-limit safety)


async def refresh_one(bot: Bot, scammer: dict) -> Optional[str]:
    """Fetch current Telegram username for one scammer and update DB.

    Returns the new username if it changed, None otherwise.
    """
    tid = scammer.get("telegram_id")
    if not tid:
        return None

    try:
        chat = await bot.get_chat(tid)
    except TelegramError as e:
        logger.debug("get_chat(%s) failed: %s", tid, e)
        return None

    new_username = chat.username  # None if user has no username set
    old_username = scammer.get("username")

    if new_username != old_username:
        logger.info(
            "Scammer #%s (ID %s) username changed: %s → %s",
            scammer["id"], tid,
            f"@{old_username}" if old_username else "—",
            f"@{new_username}" if new_username else "—",
        )
        await update_scammer_username(scammer["id"], new_username, old_username)
        return new_username
    else:
        await touch_username_check(scammer["id"])
        return None


async def refresh_batch(bot: Bot) -> int:
    """Refresh one stale batch. Returns number of usernames that changed."""
    scammers = await get_scammers_needing_refresh(
        stale_hours=REFRESH_INTERVAL_HOURS, batch=100
    )
    if not scammers:
        return 0

    changed = 0
    for scammer in scammers:
        result = await refresh_one(bot, scammer)
        if result is not None:
            changed += 1
        await asyncio.sleep(BATCH_DELAY_SECONDS)

    logger.info("Username refresh: checked %d, updated %d", len(scammers), changed)
    return changed


async def username_refresh_loop(bot: Bot) -> None:
    """Background loop — runs every REFRESH_INTERVAL_HOURS hours."""
    logger.info("Username refresh loop started (every %dh)", REFRESH_INTERVAL_HOURS)
    while True:
        try:
            await refresh_batch(bot)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Username refresh loop error: %s", e)
        await asyncio.sleep(REFRESH_INTERVAL_HOURS * 3600)
