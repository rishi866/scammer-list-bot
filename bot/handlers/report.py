"""Public /report flow — multi-step conversation."""
from __future__ import annotations

import logging
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.db import add_report

logger = logging.getLogger(__name__)

TARGET, REASON, PROOF = range(3)


async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "📋 <b>Report a Scammer</b>\n\n"
        "Step 1/3 — Who are you reporting?\n"
        "Send their <b>@username</b> or <b>Telegram ID</b>.\n\n"
        "Type /cancel to abort.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    return TARGET


async def report_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    if raw.lstrip("@").isdigit():
        context.user_data["report_target_id"] = int(raw.lstrip("@"))
        context.user_data["report_target_username"] = None
    else:
        context.user_data["report_target_id"] = None
        context.user_data["report_target_username"] = raw.lstrip("@")

    await update.message.reply_text(
        "Step 2/3 — What did they do?\n"
        "Describe the scam briefly (e.g. 'Took payment and disappeared').",
        parse_mode="HTML",
    )
    return REASON


async def report_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["report_reason"] = update.message.text.strip()
    await update.message.reply_text(
        "Step 3/3 — Any proof? (screenshot link, transaction ID, etc.)\n"
        "Type <b>none</b> if you have none.",
        parse_mode="HTML",
    )
    return PROOF


async def report_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    proof_text = update.message.text.strip()
    proof = None if proof_text.lower() == "none" else proof_text

    u = update.effective_user
    report_id = await add_report(
        reporter_id=u.id,
        reporter_username=u.username,
        target_id=context.user_data.get("report_target_id"),
        target_username=context.user_data.get("report_target_username"),
        reason=context.user_data["report_reason"],
        proof=proof,
    )

    await update.message.reply_text(
        f"✅ <b>Report #{report_id} submitted.</b>\n"
        "An admin will review it shortly. Thank you for helping keep the community safe.",
        parse_mode="HTML",
    )

    # Notify admins
    import os
    admin_ids = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
    target_display = (
        f"ID {context.user_data['report_target_id']}"
        if context.user_data.get("report_target_id")
        else f"@{context.user_data.get('report_target_username', '?')}"
    )
    notif = (
        f"📨 <b>New Report #{report_id}</b>\n"
        f"Target: {target_display}\n"
        f"Reason: {context.user_data['report_reason']}\n"
        f"Proof: {proof or '—'}\n"
        f"From: @{u.username or u.id}\n\n"
        f"Use /approve {report_id} or /reject {report_id}"
    )
    for aid in admin_ids:
        try:
            await context.bot.send_message(aid, notif, parse_mode="HTML")
        except Exception as exc:
            logger.warning("Could not notify admin %s: %s", aid, exc)

    context.user_data.clear()
    return ConversationHandler.END


async def report_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Report cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def build_report_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("report", report_start)],
        states={
            TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_target)],
            REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_reason)],
            PROOF:  [MessageHandler(filters.TEXT & ~filters.COMMAND, report_proof)],
        },
        fallbacks=[CommandHandler("cancel", report_cancel)],
    )
