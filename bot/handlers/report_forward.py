"""Report flow triggered by forwarding a message to the bot.

Any user (from any group, bot not required there) can:
  1. Forward a message from a suspected scammer to this bot
  2. Bot collects reason + optional proof photo
  3. Bot sends full report to admins with [Add as Scammer] / [Ignore] buttons

This keeps admin's personal account private and works from any group.
"""
from __future__ import annotations

import logging
import os

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ContextTypes, ConversationHandler,
    MessageHandler, CommandHandler, filters,
)

from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)

# Conversation states
FWD_REASON = 1
FWD_ID     = 2
FWD_PROOF  = 3


def _admin_ids() -> list[int]:
    return [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]


def _is_admin(user_id: int) -> bool:
    return user_id in set(_admin_ids())


def _extract_forward_user(msg):
    """Extract (user_id, username, full_name) from a forwarded message."""
    orig_user = None

    if msg.forward_origin:
        origin = msg.forward_origin
        if hasattr(origin, "sender_user") and origin.sender_user:
            orig_user = origin.sender_user

    if not orig_user and msg.forward_from:
        orig_user = msg.forward_from

    if not orig_user:
        return None, None, None

    fwd_id    = orig_user.id
    fwd_uname = orig_user.username
    fwd_name  = " ".join(filter(None, [orig_user.first_name, orig_user.last_name])) or "Unknown"
    return fwd_id, fwd_uname, fwd_name


# ── Step 1: User forwards a message ──────────────────────────────────────────

async def fwd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered when a non-admin user forwards any message in PM."""
    msg = update.message

    # Admins have their own forward handler — skip here
    if _is_admin(update.effective_user.id):
        return ConversationHandler.END

    fwd_id, fwd_uname, fwd_name = _extract_forward_user(msg)

    if not fwd_id:
        # Privacy enabled — sender hidden
        await msg.reply_text(
            em(
                "⚠️ <b>Could not identify the sender.</b>\n\n"
                "The person has their <b>Forward Privacy</b> enabled in Telegram settings, "
                "so their identity is hidden.\n\n"
                "Please use /report and enter their @username manually."
            ),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    # Save to context
    context.user_data["fwd_id"]    = fwd_id
    context.user_data["fwd_uname"] = fwd_uname
    context.user_data["fwd_name"]  = fwd_name

    uname_str = f"@{fwd_uname}" if fwd_uname else "—"
    await msg.reply_text(
        em(
            f"📨 <b>Report Submission</b>\n\n"
            f"You're reporting:\n"
            f"  👤 Name     : <b>{fwd_name}</b>\n"
            f"  📝 Username : {uname_str}\n"
            f"  🔑 Tele ID  : <code>{fwd_id}</code>\n\n"
            f"⚠️ <b>What did this person do?</b>\n"
            f"Please describe the scam/fraud in detail:"
        ),
        parse_mode="HTML",
    )
    return FWD_REASON


# ── Step 2: User gives reason ─────────────────────────────────────────────────

async def fwd_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reason = update.message.text.strip()
    if len(reason) < 5:
        await update.message.reply_text(
            em("⚠️ Please give a more detailed reason (at least 5 characters).")
        )
        return FWD_REASON

    context.user_data["fwd_reason"] = reason

    # If we already got the ID from forward metadata, skip asking
    if context.user_data.get("fwd_id"):
        existing_id = context.user_data["fwd_id"]
        await update.message.reply_text(
            em(
                f"🔑 Telegram ID already detected: <code>{existing_id}</code>\n\n"
                f"If you know a <b>different/correct</b> ID, send it now.\n"
                f"Otherwise send /skip to continue."
            ),
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            em(
                "🔑 <b>What is their Telegram ID?</b>\n\n"
                "You can find it by forwarding their message to @userinfobot\n"
                "Send the number (e.g. <code>5886335494</code>), or /skip if you don't know."
            ),
            parse_mode="HTML",
        )
    return FWD_ID


# ── Step 3: User provides (or skips) Telegram ID ────────────────────────────

async def fwd_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sent a Telegram ID number."""
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text(
            em("⚠️ Please send a valid numeric Telegram ID, or /skip to continue.")
        )
        return FWD_ID

    context.user_data["fwd_id"] = int(text)
    await update.message.reply_text(
        em(f"✅ ID saved: <code>{text}</code>"),
        parse_mode="HTML",
    )
    await update.message.reply_text(
        em("📸 <b>Send proof</b> (screenshot, photo, video).\n\nNo proof? Send /skip"),
        parse_mode="HTML",
    )
    return FWD_PROOF


async def fwd_id_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User skipped the ID step."""
    await update.message.reply_text(
        em("📸 <b>Send proof</b> (screenshot, photo, video).\n\nNo proof? Send /skip"),
        parse_mode="HTML",
    )
    return FWD_PROOF


# ── Step 4a: User sends photo proof ──────────────────────────────────────────

async def fwd_proof_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    photo = update.message.photo[-1] if update.message.photo else None
    doc   = update.message.document

    proof_file_id   = None
    proof_is_photo  = False

    if photo:
        proof_file_id  = photo.file_id
        proof_is_photo = True
    elif doc:
        proof_file_id = doc.file_id

    context.user_data["fwd_proof_file_id"]  = proof_file_id
    context.user_data["fwd_proof_is_photo"] = proof_is_photo
    return await _send_to_admins(update, context)


# ── Step 4b: User skips proof ─────────────────────────────────────────────────

async def fwd_skip_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["fwd_proof_file_id"]  = None
    context.user_data["fwd_proof_is_photo"] = False
    return await _send_to_admins(update, context)


# ── Send report to all admins ─────────────────────────────────────────────────

async def _send_to_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from bot.db import add_report

    submitter = update.effective_user
    ud        = context.user_data

    fwd_id         = ud["fwd_id"]
    fwd_uname      = ud.get("fwd_uname")
    fwd_name       = ud.get("fwd_name", "Unknown")
    reason         = ud.get("fwd_reason", "No reason provided")
    proof_file_id  = ud.get("fwd_proof_file_id")
    proof_is_photo = ud.get("fwd_proof_is_photo", False)

    # Save as pending report
    report_id = await add_report(
        reporter_id      = submitter.id,
        reporter_username= submitter.username,
        target_id        = fwd_id,
        target_username  = fwd_uname,
        target_full_name = fwd_name,
        reason           = reason,
        proof            = None,
        group_chat_id    = None,
        proof_file_id    = proof_file_id,
    )

    uname_str    = f"@{fwd_uname}" if fwd_uname else "—"
    reporter_str = f"@{submitter.username}" if submitter.username else str(submitter.id)
    proof_line   = "\n📸 <b>Proof attached below</b>" if proof_file_id else "\n❌ No proof provided"

    notif = em(
        f"📨 <b>Scammer Report #{report_id}</b>\n"
        f"<i>(via message forward)</i>\n\n"
        f"🎯 <b>Reported Person:</b>\n"
        f"  👤 Name     : <b>{fwd_name}</b>\n"
        f"  📝 Username : {uname_str}\n"
        f"  🔑 Tele ID  : <code>{fwd_id}</code>\n\n"
        f"⚠️ <b>Reason:</b> {reason}{proof_line}\n\n"
        f"📤 <b>Reporter:</b> {reporter_str} (ID: <code>{submitter.id}</code>)\n\n"
        f"👇 Choose severity to add, or ignore:"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 High",   callback_data=f"approve_high:{report_id}"),
            InlineKeyboardButton("🟡 Medium", callback_data=f"approve_medium:{report_id}"),
            InlineKeyboardButton("🟢 Low",    callback_data=f"approve_low:{report_id}"),
        ],
        [
            InlineKeyboardButton("❌ Ignore", callback_data=f"reject_sub:{report_id}"),
        ],
    ])

    notified = 0
    for aid in _admin_ids():
        try:
            if proof_file_id and proof_is_photo:
                await context.bot.send_photo(
                    aid, photo=proof_file_id,
                    caption=notif, parse_mode="HTML", reply_markup=keyboard,
                )
            elif proof_file_id:
                await context.bot.send_document(
                    aid, document=proof_file_id,
                    caption=notif, parse_mode="HTML", reply_markup=keyboard,
                )
            else:
                await context.bot.send_message(
                    aid, notif, parse_mode="HTML", reply_markup=keyboard,
                )
            notified += 1
        except Exception as exc:
            logger.warning("Could not notify admin %s: %s", aid, exc)

    proof_note = " with proof 📸" if proof_file_id else ""
    await update.message.reply_text(
        em(
            f"✅ <b>Report #{report_id} submitted{proof_note}!</b>\n\n"
            f"Thank you for helping keep the community safe. 🙏\n"
            f"Our admins will review your report shortly."
        ),
        parse_mode="HTML",
    )

    # Clear user data
    for key in ["fwd_id", "fwd_uname", "fwd_name", "fwd_reason", "fwd_proof_file_id", "fwd_proof_is_photo"]:
        context.user_data.pop(key, None)

    return ConversationHandler.END


# ── Cancel ────────────────────────────────────────────────────────────────────

async def fwd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(em("❌ Report cancelled."))
    return ConversationHandler.END


# ── Build handler ─────────────────────────────────────────────────────────────

def build_report_forward_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.FORWARDED & filters.ChatType.PRIVATE,
                fwd_start,
            )
        ],
        states={
            FWD_REASON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, fwd_reason),
            ],
            FWD_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, fwd_id_received),
                CommandHandler("skip", fwd_id_skip),
            ],
            FWD_PROOF: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, fwd_proof_photo),
                CommandHandler("skip", fwd_skip_proof),
            ],
        },
        fallbacks=[CommandHandler("cancel", fwd_cancel)],
        per_user=True,
        per_chat=True,
    )
