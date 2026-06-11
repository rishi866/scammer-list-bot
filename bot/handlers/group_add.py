"""/add @username [reason] — group member submission flow.

Features:
- Duplicate detection: warns if already in DB
- Trusted reporters: auto-approve without admin review
- Photo proof: reply to a photo with /add @username reason, or send photo with /add in caption
- Severity: admin DM has [🔴 High] [🟡 Med] [🟢 Low] [❌ Reject] buttons
"""
from __future__ import annotations

import logging
import os

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot.db import (
    add_report, add_scammer, update_report_status,
    scammer_exists, is_trusted_reporter,
)
from bot.services.admins import (
    get_admin_ids as _admin_ids,
    resolve_protected_role,
    protected_block_message,
)
from bot.services.emoji_fx import em
from bot.services.broadcaster import broadcast_scammer

logger = logging.getLogger(__name__)


def _severity_keyboard(report_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 High",   callback_data=f"approve_high:{report_id}"),
            InlineKeyboardButton("🟡 Medium", callback_data=f"approve_medium:{report_id}"),
            InlineKeyboardButton("🟢 Low",    callback_data=f"approve_low:{report_id}"),
        ],
        [
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_sub:{report_id}"),
        ],
    ])


async def group_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg  = update.message
    args = context.args
    if not args:
        await msg.reply_text(
            em("📝 Usage: /add @username [reason]\n"
               "Tip: Reply to a photo with /add @username reason to include proof."),
            parse_mode="HTML",
        )
        return

    target_arg = args[0].strip()
    reason     = " ".join(args[1:]).strip() if len(args) > 1 else "No reason provided"

    # --- Photo proof -------------------------------------------------------
    proof_file_id: str | None = None

    # Case 1: /add is a reply to a photo message
    reply = msg.reply_to_message
    if reply and reply.photo:
        proof_file_id = reply.photo[-1].file_id

    # Case 2: The /add message itself has a photo (sent as caption)
    if not proof_file_id and msg.photo:
        proof_file_id = msg.photo[-1].file_id

    # --- Resolve target via Telegram ---------------------------------------
    target_id      : int | None = None
    target_username: str | None = target_arg.lstrip("@") or None
    target_name    : str        = "Unknown"

    lookup = int(target_arg.lstrip("@")) if target_arg.lstrip("@").isdigit() else f"@{target_arg.lstrip('@')}"
    try:
        chat           = await context.bot.get_chat(lookup)
        target_id      = chat.id
        target_username= chat.username or target_username
        target_name    = " ".join(filter(None, [chat.first_name, chat.last_name])) or "Unknown"
    except TelegramError as e:
        logger.warning("Could not resolve %s via Telegram: %s", lookup, e)

    submitter = update.effective_user
    group     = update.effective_chat

    # --- Protect bot owner/admins from being reported -----------------------
    role = await resolve_protected_role(target_id, target_username, bot=context.bot)
    if role:
        await msg.reply_text(em(protected_block_message(role)), parse_mode="HTML")
        return

    # --- Duplicate detection -----------------------------------------------
    dup = await scammer_exists(target_id, target_username)
    if dup:
        uname_d = f"@{dup.get('username')}" if dup.get("username") else "—"
        await msg.reply_text(
            em(
                f"⚠️ <b>Already listed!</b>\n"
                f"This person is already in the scammer database as <b>#{dup['id']}</b> ({uname_d}).\n"
                f"Use /check {target_arg} for details."
            ),
            parse_mode="HTML",
        )
        return

    # --- Create pending report -----------------------------------------------
    report_id = await add_report(
        reporter_id      = submitter.id,
        reporter_username= submitter.username,
        target_id        = target_id,
        target_username  = target_username,
        target_full_name = target_name,
        reason           = reason,
        proof            = None,
        group_chat_id    = group.id,
        proof_file_id    = proof_file_id,
    )

    # --- Trusted reporter: auto-approve ------------------------------------
    trusted = await is_trusted_reporter(submitter.id)
    if trusted:
        scammer_id = await add_scammer(
            telegram_id  = target_id,
            username     = target_username,
            name         = target_name,
            reason       = reason,
            proof        = None,
            added_by     = submitter.id,
            severity     = "medium",
            proof_file_id= proof_file_id,
        )
        await update_report_status(report_id, "approved")

        uname = f"@{target_username}" if target_username else "—"
        tid   = f"<code>{target_id}</code>" if target_id else "—"
        await msg.reply_text(
            em(
                f"✅ <b>Auto-approved!</b>\n"
                f"You're a trusted reporter — <b>#{scammer_id}</b> {uname} added directly.\n"
                f"📢 Broadcasting to all groups..."
            ),
            parse_mode="HTML",
        )
        await broadcast_scammer(
            context.bot, scammer_id, target_username, target_id, reason,
            severity="medium", skip_group_id=group.id
        )

        # Kick from all groups
        if target_id:
            from bot.handlers.callbacks import _kick_from_all_groups
            await _kick_from_all_groups(context.bot, target_id)
        return

    # --- Build admin DM notification + severity keyboard -------------------
    uname_display = f"@{target_username}" if target_username else "—"
    proof_line    = "\n📸 <b>Photo proof attached</b>" if proof_file_id else ""
    notif = em(
        f"📨 <b>Scammer Submission #{report_id}</b>\n\n"
        f"👤 <b>Target:</b>\n"
        f"  📝 Username : {uname_display}\n"
        f"  🔑 Tele ID  : <code>{target_id or '— (could not resolve)'}</code>\n"
        f"  🙍 Full Name: {target_name}\n\n"
        f"⚠️ <b>Reason:</b> {reason}{proof_line}\n\n"
        f"📤 <b>Submitted by:</b> @{submitter.username or submitter.id} "
        f"(ID: <code>{submitter.id}</code>)\n"
        f"📌 <b>Group:</b> {group.title or 'Unknown'}\n\n"
        f"👇 Select severity to approve, or reject:"
    )
    keyboard = _severity_keyboard(report_id)

    notified = 0
    for aid in _admin_ids():
        try:
            if proof_file_id:
                await context.bot.send_photo(
                    aid, photo=proof_file_id, caption=notif,
                    parse_mode="HTML", reply_markup=keyboard
                )
            else:
                await context.bot.send_message(
                    aid, notif, parse_mode="HTML", reply_markup=keyboard
                )
            notified += 1
        except Exception as exc:
            logger.warning("Could not DM admin %s: %s", aid, exc)

    if notified == 0:
        logger.error("No admins were notified for report #%s", report_id)

    proof_note = " (with photo proof)" if proof_file_id else ""
    await msg.reply_text(
        em(f"✅ Submission #{report_id} sent to admins for review{proof_note}. Thank you!"),
        parse_mode="HTML",
        reply_to_message_id=msg.message_id,
    )
