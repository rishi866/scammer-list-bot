"""Admin commands for managing trusted reporters.

/addtrusted @username   — mark a user as trusted (their /add auto-approves)
/removetrusted @username — remove trusted status
/listtrusted            — show all trusted reporters
"""
from __future__ import annotations

import logging
import os
from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot.db import add_trusted_reporter, remove_trusted_reporter, list_trusted_reporters
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)


def _admin_ids() -> set[int]:
    return {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in _admin_ids():
            await update.message.reply_text(em("⛔ Admins only."), parse_mode="HTML")
            return
        return await func(update, context)
    return wrapper


@admin_only
async def addtrusted_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /addtrusted @username  (or just the ID if no username)"""
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /addtrusted @username")
        return

    target = args[0].strip()
    user_id: int | None   = None
    username: str | None  = None

    if target.lstrip("@").isdigit():
        user_id = int(target.lstrip("@"))
    else:
        username = target.lstrip("@")
        # Try to resolve via Telegram
        try:
            chat    = await context.bot.get_chat(f"@{username}")
            user_id = chat.id
            username= chat.username or username
        except TelegramError:
            await update.message.reply_text(
                em(f"❌ Could not resolve <code>{target}</code> via Telegram.\n"
                   "Send their numeric Telegram ID instead."),
                parse_mode="HTML",
            )
            return

    await add_trusted_reporter(user_id, username, update.effective_user.id)
    uname_display = f"@{username}" if username else str(user_id)
    await update.message.reply_text(
        em(f"✅ <b>{uname_display}</b> (ID: <code>{user_id}</code>) is now a trusted reporter.\n"
           "Their /add submissions will be auto-approved."),
        parse_mode="HTML",
    )


@admin_only
async def removetrusted_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /removetrusted @username  or  /removetrusted ID")
        return

    target = args[0].strip().lstrip("@")
    # Try to match by username from our DB
    reporters = await list_trusted_reporters()
    match = next(
        (r for r in reporters
         if (target.isdigit() and r["user_id"] == int(target))
         or (r.get("username") or "").lower() == target.lower()),
        None,
    )
    if not match:
        await update.message.reply_text(em(f"❌ No trusted reporter found for <code>{target}</code>."), parse_mode="HTML")
        return

    await remove_trusted_reporter(match["user_id"])
    uname = f"@{match['username']}" if match.get("username") else str(match["user_id"])
    await update.message.reply_text(em(f"✅ Removed trusted status from {uname}."), parse_mode="HTML")


@admin_only
async def listtrusted_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reporters = await list_trusted_reporters()
    if not reporters:
        await update.message.reply_text(em("📋 No trusted reporters yet.\nUse /addtrusted @username."), parse_mode="HTML")
        return

    lines = []
    for r in reporters:
        uname = f"@{r['username']}" if r.get("username") else "—"
        lines.append(f"• {uname} | ID: <code>{r['user_id']}</code> | Added: {str(r.get('added_at',''))[:10]}")

    await update.message.reply_text(
        em(f"📋 <b>Trusted Reporters ({len(reporters)})</b>\n\n") + "\n".join(lines),
        parse_mode="HTML",
    )
