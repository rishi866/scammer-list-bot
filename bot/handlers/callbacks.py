"""Central callback query router."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot.db import get_report, update_report_status, add_scammer
from bot.handlers.scammer_list import scammer_list_page_callback
from bot.services.emoji_fx import em
from bot.services.broadcaster import broadcast_scammer

logger = logging.getLogger(__name__)

SEV_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}


async def _approve(
    update: Update, context: ContextTypes.DEFAULT_TYPE, severity: str = "medium"
) -> None:
    query     = update.callback_query
    report_id = int(query.data.split(":")[1])
    report    = await get_report(report_id)

    if not report:
        await query.edit_message_text(em("❌ Report not found."))
        return

    if report["status"] != "pending":
        status_icon = "✅" if report["status"] == "approved" else "❌"
        await query.answer(f"Already {report['status']} {status_icon}", show_alert=True)
        return

    scammer_id = await add_scammer(
        telegram_id  = report.get("target_id"),
        username     = report.get("target_username"),
        name         = report.get("target_full_name") or report.get("target_username") or "Unknown",
        reason       = report["reason"],
        proof        = report.get("proof"),
        added_by     = query.from_user.id,
        severity     = severity,
        proof_file_id= report.get("proof_file_id"),
    )
    await update_report_status(report_id, "approved")

    # Notify originating group
    group_chat_id = report.get("group_chat_id")
    sev_icon      = SEV_ICON.get(severity, "🟡")
    if group_chat_id:
        uname = f"@{report['target_username']}" if report.get("target_username") else "—"
        tid   = f"<code>{report['target_id']}</code>" if report.get("target_id") else "—"
        try:
            await context.bot.send_message(
                group_chat_id,
                em(
                    f"✅ <b>Scammer Confirmed — #{scammer_id}</b>\n\n"
                    f"📝 Username : {uname}\n"
                    f"🔑 Tele ID  : {tid}\n"
                    f"{sev_icon} Severity  : {severity.capitalize()}\n"
                    f"⚠️ Reason   : {report['reason']}\n\n"
                    f"📋 Use /scammer_list to see the full list."
                ),
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("Could not notify group %s: %s", group_chat_id, exc)

    # Broadcast to all OTHER groups
    await broadcast_scammer(
        context.bot,
        scammer_id,
        report.get("target_username"),
        report.get("target_id"),
        report["reason"],
        severity=severity,
        skip_group_id=group_chat_id,
    )

    approver  = f"@{query.from_user.username}" if query.from_user.username else str(query.from_user.id)
    suffix    = em(f"\n\n{sev_icon} <b>Approved ({severity})</b> by {approver} → Scammer #{scammer_id}")

    # The original message may be a photo (when proof was sent) — handle both
    try:
        if query.message.photo:
            await query.edit_message_caption(
                (query.message.caption or "") + suffix,
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                (query.message.text or "") + suffix,
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.warning("Could not edit admin message: %s", exc)


async def _reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query     = update.callback_query
    report_id = int(query.data.split(":")[1])
    report    = await get_report(report_id)

    if not report:
        await query.edit_message_text(em("❌ Report not found."))
        return

    if report["status"] != "pending":
        await query.answer(f"Already {report['status']}.", show_alert=True)
        return

    await update_report_status(report_id, "rejected")

    rejecter = f"@{query.from_user.username}" if query.from_user.username else str(query.from_user.id)
    suffix   = em(f"\n\n❌ <b>Rejected</b> by {rejecter}")

    try:
        if query.message.photo:
            await query.edit_message_caption(
                (query.message.caption or "") + suffix,
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                (query.message.text or "") + suffix,
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.warning("Could not edit admin message: %s", exc)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    data = query.data
    try:
        if data.startswith("approve_high:"):
            await _approve(update, context, severity="high")
        elif data.startswith("approve_medium:"):
            await _approve(update, context, severity="medium")
        elif data.startswith("approve_low:"):
            await _approve(update, context, severity="low")
        elif data.startswith("approve_sub:"):
            # Legacy / fallback: default medium
            await _approve(update, context, severity="medium")
        elif data.startswith("reject_sub:"):
            await _reject(update, context)
        elif data.startswith("sl_page:"):
            await scammer_list_page_callback(update, context)
        else:
            logger.debug("Unknown callback: %s", data)
    except Exception as exc:
        logger.error("Callback error (%s): %s", data, exc)
