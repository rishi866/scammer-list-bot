"""Central callback query router."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

import os
from bot.db import get_report, update_report_status, add_scammer, scammer_exists, list_active_bot_groups
from bot.handlers.scammer_list import scammer_list_page_callback
from bot.handlers.start import qa_check, qa_report, qa_list
from bot.handlers.appeal import appeal_approve, appeal_reject
from bot.services.emoji_fx import em
from bot.services.broadcaster import broadcast_scammer

logger = logging.getLogger(__name__)

SEV_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}


async def _kick_from_all_groups(bot, telegram_id: int, skip_group_id: int | None = None) -> int:
    """Kick (or ban if AUTO_BAN=true) scammer from every active group. Returns count."""
    if not telegram_id:
        return 0

    groups   = await list_active_bot_groups()
    auto_ban = os.getenv("AUTO_BAN", "false").lower() in ("1", "true", "yes")
    kicked   = 0

    for g in groups:
        gid = g["group_id"]
        try:
            await bot.ban_chat_member(gid, telegram_id)
            if not auto_ban:
                # Kick only (can rejoin) — ban then immediately unban
                await bot.unban_chat_member(gid, telegram_id, only_if_banned=True)
            kicked += 1
        except Exception as e:
            logger.debug("Could not kick %s from group %s: %s", telegram_id, gid, e)

    logger.info(
        "%s scammer %s from %d group(s)",
        "Banned" if auto_ban else "Kicked", telegram_id, kicked,
    )
    return kicked


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

    # Kick scammer from all groups the bot is in
    target_tg_id = report.get("target_id")

    # If ID is missing but username is available, try resolving one more time
    if not target_tg_id and report.get("target_username"):
        try:
            chat         = await context.bot.get_chat(f"@{report['target_username']}")
            target_tg_id = chat.id
            # Save resolved ID to DB for future
            from bot.db import update_scammer_telegram_id
            await update_scammer_telegram_id(scammer_id, chat.id, chat.username)
            logger.info("Resolved telegram_id=%s for scammer #%s at approval time", chat.id, scammer_id)
        except Exception as e:
            logger.warning("Could not resolve ID for @%s at kick time: %s", report.get("target_username"), e)

    if target_tg_id:
        kicked = await _kick_from_all_groups(context.bot, target_tg_id)
        auto_ban = os.getenv("AUTO_BAN", "false").lower() in ("1", "true", "yes")
        action   = "🔨 Banned" if auto_ban else "🦵 Kicked"
        if kicked and group_chat_id:
            try:
                await context.bot.send_message(
                    group_chat_id,
                    em(f"{action} <b>@{report.get('target_username') or target_tg_id}</b> from {kicked} group(s)."),
                    parse_mode="HTML",
                )
            except Exception:
                pass
    else:
        # Still no ID — warn in the group
        if group_chat_id and report.get("target_username"):
            try:
                await context.bot.send_message(
                    group_chat_id,
                    em(
                        f"⚠️ <b>Could not kick @{report['target_username']}</b>\n"
                        f"Telegram ID unknown — forward any message from them to me (bot PM) to resolve it.\n"
                        f"Then use /fixids or I'll auto-capture next time they join a group."
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

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


async def _quickadd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """quickadd:<telegram_id>:<username> — quick-add from forwarded message."""
    query  = update.callback_query
    parts  = query.data.split(":", 2)
    tg_id  = int(parts[1])
    uname  = parts[2] if len(parts) > 2 and parts[2] else None

    # Double-check duplicate
    dup = await scammer_exists(tg_id, uname)
    if dup:
        await query.edit_message_text(
            em(f"⚠️ Already listed as <b>#{dup['id']}</b>."),
            parse_mode="HTML",
        )
        return

    # Add with basic info — admin can update reason later with /remove + /addid
    scammer_id = await add_scammer(
        telegram_id = tg_id,
        username    = uname,
        name        = uname or str(tg_id),
        reason      = "Added via forward",
        proof       = None,
        added_by    = query.from_user.id,
        severity    = "medium",
    )

    uname_str = f"@{uname}" if uname else "—"
    await query.edit_message_text(
        em(
            f"✅ <b>Scammer #{scammer_id} added!</b>\n\n"
            f"🔑 ID       : <code>{tg_id}</code>\n"
            f"📝 Username : {uname_str}\n"
            f"⚠️ Reason   : Added via forward\n\n"
            f"💡 Update reason: /remove {scammer_id} → re-add with /addid"
        ),
        parse_mode="HTML",
    )

    await broadcast_scammer(
        context.bot, scammer_id, uname, tg_id,
        "Added via forward", severity="medium",
    )

    # Kick from all groups immediately
    kicked   = await _kick_from_all_groups(context.bot, tg_id)
    auto_ban = os.getenv("AUTO_BAN", "false").lower() in ("1", "true", "yes")
    action   = "🔨 Banned" if auto_ban else "🦵 Kicked"
    if kicked:
        await query.message.reply_text(
            em(f"{action} <b>{uname_str or tg_id}</b> from {kicked} group(s)."),
            parse_mode="HTML",
        )


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
        elif data.startswith("quickadd:"):
            await _quickadd(update, context)
        elif data == "qa_check":
            await qa_check(update, context)
        elif data == "qa_report":
            await qa_report(update, context)
        elif data == "qa_list":
            await qa_list(update, context)
        elif data.startswith("appeal_approve:"):
            await appeal_approve(update, context)
        elif data.startswith("appeal_reject:"):
            await appeal_reject(update, context)
        else:
            logger.debug("Unknown callback: %s", data)
    except Exception as exc:
        logger.error("Callback error (%s): %s", data, exc)
