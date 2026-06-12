"""Owner-only admin management.

/addadmin <telegram_id>     — grant admin access (owner only)
/removeadmin <telegram_id>  — revoke admin access (owner only)
/listadmins                 — show owner + all admins (any admin)
/webpass <password>         — set/change your web admin panel login (any admin)

Only the bot OWNER (see bot.services.admins.owner_id — OWNER_ID env var,
falling back to the first ADMIN_IDS entry) can add or remove admins. Admins
added this way take effect immediately (no restart) via the in-memory cache
in bot.services.admins.

/webpass lets any admin (including the owner) set their own login for the
web admin panel (bot.services.web_admin) — each admin gets a private
username/password instead of sharing WEB_ADMIN_USER/WEB_ADMIN_PASS.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot.db import set_web_credentials, delete_web_credentials
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
from bot.services.webauth import hash_password

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
        await delete_web_credentials(target_id)
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


_USAGE_WEBPASS = (
    "🔒 <b>Set your web panel login</b>\n\n"
    "<code>/webpass &lt;new password&gt;</code>\n\n"
    "Minimum 6 characters. This sets (or changes) the password for your "
    "personal login to the web admin panel — your username is your "
    "Telegram @handle (or <code>id&lt;your id&gt;</code> if you have no "
    "username)."
)


@admin_only
async def webpass_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            em("🔒 Please use /webpass in a private chat with the bot — don't post passwords in groups."),
            parse_mode="HTML",
        )
        return

    args = context.args
    if not args or len(args[0]) < 6:
        await update.message.reply_text(em(_USAGE_WEBPASS), parse_mode="HTML")
        return

    user = update.effective_user
    web_username   = user.username or f"id{user.id}"
    password_hash, salt = hash_password(args[0])

    try:
        await set_web_credentials(user.id, web_username, password_hash, salt)
    except Exception as e:
        logger.warning("set_web_credentials failed for %s: %s", user.id, e)
        await update.message.reply_text(
            em("⚠️ Could not save your web login — that username may already be taken. Contact the owner."),
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        em(
            f"✅ <b>Web panel login saved!</b>\n\n"
            f"👤 Username: <code>{web_username}</code>\n"
            f"🔑 Password: (the one you just sent)\n\n"
            f"Use these with the web admin panel's login prompt (HTTP Basic Auth)."
        ),
        parse_mode="HTML",
    )
    await audit(update.effective_user, "webpass", "admin", user.id, "set web panel login")
