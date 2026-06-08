"""Auto-check new members against the scammer list on group join.

If AUTO_BAN=true in .env and the bot is admin, confirmed scammers are
automatically banned. Otherwise just an alert is sent.
"""
from __future__ import annotations

import logging
import os

from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot.db import search_by_telegram_id, search_by_username, update_scammer_telegram_id
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)

SEV_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}


async def on_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires when a user's status in a group changes to member/administrator."""
    event = update.chat_member
    if not event:
        return

    new_status = event.new_chat_member.status
    if new_status not in ("member", "administrator"):
        return

    user  = event.new_chat_member.user
    chat  = event.chat

    # Skip bots
    if user.is_bot:
        return

    # Check by Telegram ID first (fastest)
    results = await search_by_telegram_id(user.id)

    # If not found by ID, try matching by current username
    if not results and user.username:
        results = await search_by_username(user.username)

    if not results:
        return

    # If we found them by username but DB has no telegram_id → update it now
    # (scammer joined a group, so we have their real ID directly from the event)
    for entry in results:
        if not entry.get("telegram_id") and user.id:
            await update_scammer_telegram_id(entry["id"], user.id, user.username)
            entry["telegram_id"] = user.id
            logger.info(
                "Auto-resolved telegram_id=%s for scammer #%s (@%s) on group join",
                user.id, entry["id"], user.username,
            )

    # Build alert
    e = results[0]
    sev      = (e.get("severity") or "medium").lower()
    sev_icon = SEV_ICON.get(sev, "🟡")
    uname    = f"@{e['username']}" if e.get("username") else f"@{user.username or user.id}"
    history  = [u for u in (e.get("username_history") or []) if u]
    hist_str = ", ".join(f"@{u}" for u in history) if history else "—"

    alert = em(
        f"🚨 <b>Warning: Known Scammer Joined!</b>\n\n"
        f"👤 User : {uname} (ID: <code>{user.id}</code>)\n"
        f"{sev_icon} Severity: {sev.capitalize()}\n"
        f"⚠️ Reason: {e['reason']}\n"
        f"🔄 Past usernames: {hist_str}\n"
        f"📋 Listed as scammer <b>#{e['id']}</b>\n\n"
        f"👮 Admins, please take action."
    )

    try:
        await context.bot.send_message(chat.id, alert, parse_mode="HTML")
    except TelegramError as err:
        logger.warning("Could not send join alert in %s: %s", chat.id, err)
        return

    # Auto-ban if enabled
    auto_ban = os.getenv("AUTO_BAN", "false").lower() in ("1", "true", "yes")
    if auto_ban:
        try:
            await context.bot.ban_chat_member(chat.id, user.id)
            await context.bot.send_message(
                chat.id,
                em(f"🔨 <b>Auto-banned</b> scammer #{e['id']} ({uname})."),
                parse_mode="HTML",
            )
            logger.info("Auto-banned scammer %s from group %s", user.id, chat.id)
        except TelegramError as err:
            logger.warning("Auto-ban failed for %s in %s: %s", user.id, chat.id, err)
