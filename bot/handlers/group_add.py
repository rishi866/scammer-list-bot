"""/add @username [reason] — group member submission flow."""
from __future__ import annotations

import logging
import os

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot.db import add_report
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)


def _admin_ids() -> set[int]:
    return {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}


async def group_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            em("📝 Usage: /add @username [reason]\nExample: /add @scammer123 Took payment and vanished")
        )
        return

    target_arg = args[0].strip()
    reason     = " ".join(args[1:]).strip() if len(args) > 1 else "No reason provided"

    target_id       : int | None = None
    target_username : str | None = target_arg.lstrip("@") or None
    target_name     : str        = "Unknown"

    lookup = int(target_arg.lstrip("@")) if target_arg.lstrip("@").isdigit() else f"@{target_arg.lstrip('@')}"
    try:
        chat            = await context.bot.get_chat(lookup)
        target_id       = chat.id
        target_username = chat.username or target_username
        target_name     = " ".join(filter(None, [chat.first_name, chat.last_name])) or "Unknown"
    except TelegramError as e:
        logger.warning("Could not resolve %s via Telegram: %s", lookup, e)

    submitter = update.effective_user
    group     = update.effective_chat

    report_id = await add_report(
        reporter_id      = submitter.id,
        reporter_username= submitter.username,
        target_id        = target_id,
        target_username  = target_username,
        target_full_name = target_name,
        reason           = reason,
        proof            = None,
        group_chat_id    = group.id,
    )

    uname_display = f"@{target_username}" if target_username else "—"
    notif = em(
        f"📨 <b>Scammer Submission #{report_id}</b>\n\n"
        f"👤 <b>Target (from Telegram):</b>\n"
        f"  📝 Username : {uname_display}\n"
        f"  🔑 Tele ID  : <code>{target_id or '— (could not resolve)'}</code>\n"
        f"  🙍 Full Name: {target_name}\n\n"
        f"⚠️ <b>Reason:</b> {reason}\n\n"
        f"📤 <b>Submitted by:</b> @{submitter.username or submitter.id} "
        f"(ID: <code>{submitter.id}</code>)\n"
        f"📌 <b>Group:</b> {group.title or 'Unknown'}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_sub:{report_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject_sub:{report_id}"),
    ]])

    notified = 0
    for aid in _admin_ids():
        try:
            await context.bot.send_message(aid, notif, parse_mode="HTML", reply_markup=keyboard)
            notified += 1
        except Exception as exc:
            logger.warning("Could not DM admin %s: %s", aid, exc)

    if notified == 0:
        logger.error("No admins were notified for report #%s", report_id)

    await update.message.reply_text(
        em(f"✅ Submission #{report_id} sent to admins for review. Thank you!"),
        reply_to_message_id=update.message.message_id,
    )
