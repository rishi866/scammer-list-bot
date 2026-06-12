"""Owner-only admin management.

/addadmin <telegram_id>     — grant admin access (owner only)
/removeadmin <telegram_id>  — revoke admin access (owner only)
/listadmins                 — show owner + all admins (any admin)

Only the bot OWNER (see bot.services.admins.owner_id — OWNER_ID env var,
falling back to the first ADMIN_IDS entry) can add or remove admins. Admins
added this way take effect immediately (no restart) via the in-memory cache
in bot.services.admins.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot.handlers.admin import admin_only
from bot.services.admins import (
    add_admin,
    remove_admin,
    list_all_admins,
    is_owner,
    owner_only,
)
from bot.services.audit import audit
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)

_SOURCE_TAG = {"owner": "👑 Owner", "env": "🔧 Admin (.env)", "db": "🛠 Admin"}

_USAGE_ADD = (
    "🔒 <b>Usage</b>\n\n"
    "<code>/addadmin &lt;telegram_id&gt;</code>\n\n"
    "Get someone's numeric Telegram ID from the \"🆕 New User Joined\" "
    "notification you get when they /start the bot, or via @userinfobot."
)


@owner_only
async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text(em(_USAGE_ADD), parse_mode="HTML")
        return

    target_id = int(args[0])

    if is_owner(target_id):
        await update.message.reply_text(em("ℹ️ That user is already the owner."), parse_mode="HTML")
        return

    added = await add_admin(target_id, update.effective_user.id)
    if not added:
        await update.message.reply_text(em(f"ℹ️ <code>{target_id}</code> is already an admin."), parse_mode="HTML")
        return

    await update.message.reply_text(
        em(
            f"✅ <code>{target_id}</code> is now an <b>admin</b>.\n\n"
            "They can use admin commands (/pending, /approve, /edit, etc.) "
            "but cannot add/remove other admins — only you (the owner) can."
        ),
        parse_mode="HTML",
    )
    await audit(update.effective_user, "addadmin", "admin", target_id)

    try:
        await context.bot.send_message(
            target_id,
            em("🎉 You've been made an <b>admin</b> of Scammer List Bot!\n\nSend /start to see admin tools."),
            parse_mode="HTML",
        )
    except TelegramError:
        pass


@owner_only
async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text(
            em("Usage: <code>/removeadmin &lt;telegram_id&gt;</code>  (see /listadmins)"),
            parse_mode="HTML",
        )
        return

    target_id = int(args[0])
    result    = await remove_admin(target_id)

    if result == "removed":
        await update.message.reply_text(em(f"✅ <code>{target_id}</code> is no longer an admin."), parse_mode="HTML")
        await audit(update.effective_user, "removeadmin", "admin", target_id)
    elif result == "owner":
        await update.message.reply_text(em("⛔ You can't remove the owner."), parse_mode="HTML")
    elif result == "env":
        await update.message.reply_text(
            em(
                f"⚠️ <code>{target_id}</code> is set via the server's "
                "<code>ADMIN_IDS</code> in .env, not added with /addadmin — "
                "remove it from .env and restart the bot to revoke."
            ),
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(em(f"❌ <code>{target_id}</code> is not an admin."), parse_mode="HTML")


@admin_only
async def listadmins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admins = await list_all_admins()

    lines = []
    for a in admins:
        tag = _SOURCE_TAG.get(a["source"], "Admin")
        lines.append(f"{tag} — <code>{a['telegram_id']}</code>")

    await update.message.reply_text(
        em(f"👥 <b>Bot Admins ({len(admins)})</b>\n\n")
        + "\n".join(lines)
        + em("\n\nOnly the 👑 owner can /addadmin or /removeadmin."),
        parse_mode="HTML",
    )
