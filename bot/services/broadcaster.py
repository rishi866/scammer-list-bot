"""Cross-group broadcaster + bot group tracker.

Tracks every group the bot is added to (bot_groups table).
On scammer approval, broadcasts an alert to ALL active groups.
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import Bot, Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot.db import upsert_bot_group, deactivate_bot_group, list_active_bot_groups
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)


async def on_bot_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Track when bot is added to or removed from a group."""
    result = update.my_chat_member
    if not result:
        return

    chat   = result.chat
    new_st = result.new_chat_member.status

    if chat.type not in ("group", "supergroup"):
        return

    if new_st in ("member", "administrator"):
        await upsert_bot_group(chat.id, chat.title)
        logger.info("Bot added to group: %s (%s)", chat.title, chat.id)
    elif new_st in ("left", "kicked", "banned", "restricted"):
        await deactivate_bot_group(chat.id)
        logger.info("Bot removed from group: %s (%s)", chat.title, chat.id)


async def broadcast_scammer(
    bot: Bot,
    scammer_id: int,
    username: Optional[str],
    telegram_id: Optional[int],
    reason: str,
    severity: str = "medium",
    skip_group_id: Optional[int] = None,
) -> int:
    """Send scammer confirmed alert to all active groups. Returns count sent."""

    groups = await list_active_bot_groups()
    if not groups:
        return 0

    sev_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(severity, "🟡")
    uname    = f"@{username}" if username else "—"
    tid      = f"<code>{telegram_id}</code>" if telegram_id else "—"

    text = em(
        f"🚨 <b>Scammer Alert — #{scammer_id}</b>\n\n"
        f"📝 Username : {uname}\n"
        f"🔑 Tele ID  : {tid}\n"
        f"{sev_icon} Severity  : {severity.capitalize()}\n"
        f"⚠️ Reason   : {reason}\n\n"
        f"📋 Use /scammer_list to see the full list.\n"
        f"🔍 Use /check @username to verify anyone."
    )

    sent = 0
    for g in groups:
        gid = g["group_id"]
        if gid == skip_group_id:
            continue
        try:
            await bot.send_message(gid, text, parse_mode="HTML")
            sent += 1
        except TelegramError as e:
            logger.warning("Could not broadcast to group %s: %s", gid, e)
            if "bot was kicked" in str(e).lower() or "chat not found" in str(e).lower():
                await deactivate_bot_group(gid)

    logger.info("Broadcast sent to %d/%d groups", sent, len(groups))
    return sent
