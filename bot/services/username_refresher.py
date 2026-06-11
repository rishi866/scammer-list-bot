"""Auto-update scammer usernames/names via Telegram's getChat API.

How it works
------------
Telegram IDs never change, but usernames can.  When a scammer changes
their username after a scam, a lookup by @username fails.  By storing the
ID and periodically calling bot.get_chat(id) we always have the latest
username — and keep the full history of old ones.

Fallback: get_chat(@username)
------------------------------
bot.get_chat(user_id) (raw numeric ID) only works when Telegram has an
"access hash" for that user cached for our bot — i.e. the user has messaged
the bot, or the bot has otherwise seen them in a group. Scammers are often
kicked/banned, so this commonly fails even for entries that already have a
username on file.

bot.get_chat("@username") is a different, *global* lookup (the public
username directory) and often succeeds even when the ID-based lookup
doesn't. If it resolves to the SAME telegram_id, we use it to refresh the
display name. If it resolves to a DIFFERENT id, the username has likely
been reassigned/changed — we just log it for an admin to investigate.

Limitations
-----------
If get_chat() fails both ways (no access AND no/changed username), we skip
silently and retry on the next cycle.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Bot
from telegram.error import TelegramError

from bot.db import (
    update_scammer_username,
    update_scammer_field,
    touch_username_check,
    get_scammers_needing_refresh,
)

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_HOURS = 6   # how often the background loop runs
BATCH_DELAY_SECONDS    = 1   # pause between individual getChat calls (rate-limit safety)


async def refresh_one(bot: Bot, scammer: dict) -> bool:
    """Fetch current Telegram username/name for one scammer and update DB.

    Returns True if anything (username or name) changed, False otherwise.
    """
    tid   = scammer.get("telegram_id")
    uname = scammer.get("username")
    if not tid:
        return False

    chat = None
    try:
        chat = await bot.get_chat(tid)
    except TelegramError as e:
        logger.debug("get_chat(%s) failed: %s", tid, e)

    if chat is None and uname:
        # ID-based lookup failed but we have a username on file — try the
        # global username directory instead (see module docstring).
        try:
            by_uname = await bot.get_chat(f"@{uname}")
            if by_uname.id == tid:
                chat = by_uname
            else:
                logger.info(
                    "Scammer #%s: @%s now belongs to a different account "
                    "(ID %s, expected %s) — they may have changed usernames.",
                    scammer["id"], uname, by_uname.id, tid,
                )
        except TelegramError as e:
            logger.debug("get_chat(@%s) failed: %s", uname, e)

    if chat is None:
        return False

    new_username = chat.username  # None if user has no username set
    old_username = scammer.get("username")
    new_name      = " ".join(filter(None, [chat.first_name, chat.last_name])) or None
    old_name      = scammer.get("name")

    changed = False

    if new_username != old_username:
        logger.info(
            "Scammer #%s (ID %s) username changed: %s → %s",
            scammer["id"], tid,
            f"@{old_username}" if old_username else "—",
            f"@{new_username}" if new_username else "—",
        )
        await update_scammer_username(scammer["id"], new_username, old_username)
        changed = True

    if new_name and new_name != old_name:
        await update_scammer_field(scammer["id"], "name", new_name)
        changed = True

    if not changed:
        await touch_username_check(scammer["id"])

    return changed


async def refresh_batch(bot: Bot) -> int:
    """Refresh one stale batch. Returns number of scammers that changed."""
    scammers = await get_scammers_needing_refresh(
        stale_hours=REFRESH_INTERVAL_HOURS, batch=100
    )
    if not scammers:
        return 0

    changed = 0
    for scammer in scammers:
        if await refresh_one(bot, scammer):
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
