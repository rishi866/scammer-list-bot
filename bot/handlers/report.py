"""Public /report flow — multi-step conversation (private chat)."""
from __future__ import annotations

import logging
import os

from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.db import add_report, scammer_exists
from bot.services.admins import get_admin_ids, resolve_protected_role, protected_block_message
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)

TARGET, REASON, PROOF = range(3)


async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        em(
            "📨 <b>Report a Scammer</b>\n\n"
            "Step 1/3 — Who are you reporting?\n"
            "Send their <b>@username</b> or <b>Telegram ID</b>.\n\n"
            "Type /cancel to abort."
        ),
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    return TARGET


async def report_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    if raw.lstrip("@").isdigit():
        target_id       = int(raw.lstrip("@"))
        target_username = None
    else:
        target_id       = None
        target_username = raw.lstrip("@")

    # Protect bot owner/admins from being reported
    role = await resolve_protected_role(target_id, target_username, bot=context.bot)
    if role:
        await update.message.reply_text(em(protected_block_message(role)), parse_mode="HTML")
        context.user_data.clear()
        return ConversationHandler.END

    # Already a confirmed scammer — no need to file another report
    dup = await scammer_exists(target_id, target_username)
    if dup:
        sev      = (dup.get("severity") or "medium").lower()
        sev_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(sev, "🟡")
        uname    = f"@{dup['username']}" if dup.get("username") else "—"
        await update.message.reply_text(
            em(
                f"ℹ️ <b>Already listed as Scammer #{dup['id']}</b>\n\n"
                f"📝 Username : {uname}\n"
                f"🔑 Tele ID  : <code>{dup.get('telegram_id') or '—'}</code>\n"
                f"{sev_icon} Severity  : {sev.capitalize()}\n"
                f"⚠️ Reason   : {dup['reason']}\n\n"
                f"No need to report again — use /check to verify anyone."
            ),
            parse_mode="HTML",
        )
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["report_target_id"]      = target_id
    context.user_data["report_target_username"] = target_username

    await update.message.reply_text(
        em("⚠️ Step 2/3 — What did they do?\nDescribe the scam briefly."),
        parse_mode="HTML",
    )
    return REASON


async def report_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["report_reason"] = update.message.text.strip()
    await update.message.reply_text(
        em("🔗 Step 3/3 — Any proof? (link, screenshot, transaction ID)\nType <b>none</b> if you have none."),
        parse_mode="HTML",
    )
    return PROOF


async def report_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    proof_text = update.message.text.strip()
    proof = None if proof_text.lower() == "none" else proof_text

    u = update.effective_user
    report_id = await add_report(
        reporter_id      = u.id,
        reporter_username= u.username,
        target_id        = context.user_data.get("report_target_id"),
        target_username  = context.user_data.get("report_target_username"),
        reason           = context.user_data["report_reason"],
        proof            = proof,
    )

    await update.message.reply_text(
        em(f"✅ <b>Report #{report_id} submitted!</b>\nAn admin will review it shortly. Thank you."),
        parse_mode="HTML",
    )

    # Notify admins
    admin_ids = get_admin_ids()
    target_display = (
        f"ID {context.user_data['report_target_id']}"
        if context.user_data.get("report_target_id")
        else f"@{context.user_data.get('report_target_username', '?')}"
    )
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    notif = em(
        f"📨 <b>New Report #{report_id}</b>\n\n"
        f"👤 Target: {target_display}\n"
        f"⚠️ Reason: {context.user_data['report_reason']}\n"
        f"🔗 Proof: {proof or '—'}\n"
        f"📤 From: @{u.username or u.id}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_sub:{report_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject_sub:{report_id}"),
    ]])
    for aid in admin_ids:
        try:
            await context.bot.send_message(aid, notif, parse_mode="HTML", reply_markup=keyboard)
        except Exception as exc:
            logger.warning("Could not notify admin %s: %s", aid, exc)

    context.user_data.clear()
    return ConversationHandler.END


async def report_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(em("❌ Report cancelled."), parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def build_report_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("report", report_start, filters=filters.ChatType.PRIVATE)],
        states={
            TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_target)],
            REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_reason)],
            PROOF:  [MessageHandler(filters.TEXT & ~filters.COMMAND, report_proof)],
        },
        fallbacks=[CommandHandler("cancel", report_cancel)],
    )
