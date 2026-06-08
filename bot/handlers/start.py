import os
import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError
from bot.db import search_by_telegram_id
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)

_WELCOME = (
    "✨ <b>Scammer List Bot</b>\n\n"
    "Report and verify Telegram scammers to keep the community safe.\n\n"
    "<b>Commands:</b>\n"
    "🔍 /check — Check if someone is a known scammer\n"
    "📝 /add @username — Submit a scammer (in group)\n"
    "📋 /scammer_list — View all confirmed scammers\n"
    "📨 /report — Report via private chat\n"
    "ℹ️ /help — Show this message"
)

SEV_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def _admin_ids() -> list[int]:
    return [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user      = update.effective_user
    full_name = " ".join(filter(None, [user.first_name, user.last_name])) or "—"
    username  = f"@{user.username}" if user.username else "—"

    # Send welcome to user
    await update.message.reply_text(em(_WELCOME), parse_mode="HTML")

    # Check if this user is a known scammer
    scammer_records = await search_by_telegram_id(user.id)

    if scammer_records:
        # 🚨 SCAMMER ALERT to admins
        e        = scammer_records[0]
        sev      = (e.get("severity") or "medium").lower()
        sev_icon = SEV_ICON.get(sev, "🟡")
        history  = [u for u in (e.get("username_history") or []) if u]
        hist_str = ", ".join(f"@{u}" for u in history) if history else "—"

        notif = (
            f"🚨 <b>ALERT: Known Scammer Started the Bot!</b>\n\n"
            f"👤 Name     : <b>{full_name}</b>\n"
            f"📝 Username : {username}\n"
            f"🔑 User ID  : <code>{user.id}</code>\n\n"
            f"📋 <b>Listed as Scammer #{e['id']}</b>\n"
            f"{sev_icon} Severity  : {sev.capitalize()}\n"
            f"⚠️ Reason   : {e['reason']}\n"
            f"🔄 Past usernames: {hist_str}"
        )
    else:
        # Normal new user notification
        notif = (
            f"🆕 <b>New User Joined</b>\n\n"
            f"👤 Name    : <b>{full_name}</b>\n"
            f"📝 Username: {username}\n"
            f"🔑 User ID : <code>{user.id}</code>"
        )

    for aid in _admin_ids():
        if aid == user.id:
            continue
        try:
            await context.bot.send_message(aid, notif, parse_mode="HTML")
        except TelegramError as e:
            logger.warning("Could not notify admin %s: %s", aid, e)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(em(_WELCOME), parse_mode="HTML")
