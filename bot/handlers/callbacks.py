"""Central callback query router.

Routes:
  approve_sub:<report_id>  — admin approves a member submission
  reject_sub:<report_id>   — admin rejects a member submission
  sl_page:<page_number>    — navigate scammer_list pages
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot.db import get_report, update_report_status, add_scammer
from bot.handlers.scammer_list import scammer_list_page_callback

logger = logging.getLogger(__name__)


async def _approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query     = update.callback_query
    report_id = int(query.data.split(":")[1])
    report    = await get_report(report_id)

    if not report:
        await query.edit_message_text("❌ Report not found.")
        return

    if report["status"] != "pending":
        status_icon = "✅" if report["status"] == "approved" else "❌"
        await query.answer(f"Already {report['status']} {status_icon}", show_alert=True)
        return

    scammer_id = await add_scammer(
        telegram_id = report.get("target_id"),
        username    = report.get("target_username"),
        name        = report.get("target_full_name") or report.get("target_username") or "Unknown",
        reason      = report["reason"],
        proof       = report.get("proof"),
        added_by    = query.from_user.id,
    )
    await update_report_status(report_id, "approved")

    # Notify group that the scammer is now confirmed
    group_chat_id = report.get("group_chat_id")
    if group_chat_id:
        uname = f"@{report['target_username']}" if report.get("target_username") else "—"
        tid   = f"<code>{report['target_id']}</code>" if report.get("target_id") else "—"
        try:
            await context.bot.send_message(
                group_chat_id,
                f"✅ <b>Scammer Confirmed — #{scammer_id}</b>\n\n"
                f"Username : {uname}\n"
                f"Tele ID  : {tid}\n"
                f"Reason   : {report['reason']}\n\n"
                f"Use /scammer_list to see the full list.",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("Could not notify group %s: %s", group_chat_id, exc)

    approver = f"@{query.from_user.username}" if query.from_user.username else str(query.from_user.id)
    await query.edit_message_text(
        query.message.text
        + f"\n\n✅ <b>Approved</b> by {approver} → Scammer #{scammer_id}",
        parse_mode="HTML",
    )


async def _reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query     = update.callback_query
    report_id = int(query.data.split(":")[1])
    report    = await get_report(report_id)

    if not report:
        await query.edit_message_text("❌ Report not found.")
        return

    if report["status"] != "pending":
        await query.answer(f"Already {report['status']}.", show_alert=True)
        return

    await update_report_status(report_id, "rejected")

    rejecter = f"@{query.from_user.username}" if query.from_user.username else str(query.from_user.id)
    await query.edit_message_text(
        query.message.text + f"\n\n❌ <b>Rejected</b> by {rejecter}",
        parse_mode="HTML",
    )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    data = query.data
    try:
        if data.startswith("approve_sub:"):
            await _approve(update, context)
        elif data.startswith("reject_sub:"):
            await _reject(update, context)
        elif data.startswith("sl_page:"):
            await scammer_list_page_callback(update, context)
        else:
            logger.debug("Unknown callback: %s", data)
    except Exception as exc:
        logger.error("Callback error (%s): %s", data, exc)
