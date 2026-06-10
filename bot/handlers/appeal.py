"""/appeal — let a listed user dispute their scammer entry (PM only).

Flow:
  1. User (who is in the scammer list under their own Telegram ID) sends
     /appeal <reason>
  2. Bot creates a pending appeal row + DMs all admins with the appeal text
     and [✅ Approve & Remove] / [❌ Reject] buttons.
  3. Admin taps a button → scammer is removed (if approved) or the appeal is
     dismissed (if rejected). The user is notified of the outcome either way.
"""
from __future__ import annotations

import logging
import os

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, CommandHandler, filters
from telegram.error import TelegramError

from bot.db import (
    search_by_telegram_id,
    get_scammer_by_id,
    add_appeal,
    get_appeal,
    get_pending_appeal_for_scammer,
    update_appeal_status,
    remove_scammer,
)
from bot.services.admins import get_admin_ids as _admin_ids
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)


async def appeal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/appeal <reason> — only usable by someone currently listed as a scammer."""
    user = update.effective_user

    entries = await search_by_telegram_id(user.id)
    if not entries:
        await update.message.reply_text(
            em("✅ You're not in the scammer list — no appeal needed."),
            parse_mode="HTML",
        )
        return

    args = context.args
    if not args or len(" ".join(args).strip()) < 5:
        await update.message.reply_text(
            em(
                "⚖️ <b>Appeal Your Listing</b>\n\n"
                "You're currently listed as a scammer. If you believe this is a "
                "mistake, explain why (min. 5 characters):\n\n"
                "<code>/appeal I am not the person who did this because...</code>"
            ),
            parse_mode="HTML",
        )
        return

    e = entries[0]

    existing = await get_pending_appeal_for_scammer(e["id"])
    if existing:
        await update.message.reply_text(
            em(f"ℹ️ You already have a pending appeal (#{existing['id']}). Please wait for admin review."),
            parse_mode="HTML",
        )
        return

    message   = " ".join(args).strip()
    appeal_id = await add_appeal(
        scammer_id  = e["id"],
        telegram_id = user.id,
        username    = user.username,
        message     = message,
    )

    await update.message.reply_text(
        em(f"✅ <b>Appeal #{appeal_id} submitted.</b>\nAn admin will review it and respond soon."),
        parse_mode="HTML",
    )

    uname = f"@{e['username']}" if e.get("username") else "—"
    notif = em(
        f"⚖️ <b>New Appeal #{appeal_id}</b>\n\n"
        f"📋 Listed as Scammer #{e['id']} ({uname}, ID <code>{user.id}</code>)\n"
        f"⚠️ Listed reason: {e['reason']}\n\n"
        f"💬 <b>Their appeal:</b>\n{message}\n\n"
        f"👇 Approve to remove from the list, or reject:"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve & Remove", callback_data=f"appeal_approve:{appeal_id}"),
        InlineKeyboardButton("❌ Reject",           callback_data=f"appeal_reject:{appeal_id}"),
    ]])
    for aid in _admin_ids():
        try:
            await context.bot.send_message(aid, notif, parse_mode="HTML", reply_markup=keyboard)
        except Exception as exc:
            logger.warning("Could not notify admin %s about appeal #%s: %s", aid, appeal_id, exc)


# ── Callback handlers (wired via callbacks.callback_router) ──────────────────

async def appeal_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query     = update.callback_query
    appeal_id = int(query.data.split(":")[1])
    appeal    = await get_appeal(appeal_id)

    if not appeal:
        await query.answer("Appeal not found.", show_alert=True)
        return
    if appeal["status"] != "pending":
        await query.answer(f"Already {appeal['status']}.", show_alert=True)
        return

    scammer = await get_scammer_by_id(appeal["scammer_id"])
    await update_appeal_status(appeal_id, "approved")
    if scammer:
        await remove_scammer(appeal["scammer_id"])

    approver = f"@{query.from_user.username}" if query.from_user.username else str(query.from_user.id)
    suffix   = em(f"\n\n✅ <b>Approved</b> by {approver} — scammer #{appeal['scammer_id']} removed.")
    try:
        if query.message.photo:
            await query.edit_message_caption((query.message.caption or "") + suffix, parse_mode="HTML")
        else:
            await query.edit_message_text((query.message.text or "") + suffix, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Could not edit appeal message: %s", exc)

    if appeal.get("telegram_id"):
        try:
            await context.bot.send_message(
                appeal["telegram_id"],
                em("✅ <b>Your appeal was approved.</b>\nYou've been removed from the scammer list."),
                parse_mode="HTML",
            )
        except TelegramError as exc:
            logger.debug("Could not notify user %s of appeal approval: %s", appeal["telegram_id"], exc)


async def appeal_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query     = update.callback_query
    appeal_id = int(query.data.split(":")[1])
    appeal    = await get_appeal(appeal_id)

    if not appeal:
        await query.answer("Appeal not found.", show_alert=True)
        return
    if appeal["status"] != "pending":
        await query.answer(f"Already {appeal['status']}.", show_alert=True)
        return

    await update_appeal_status(appeal_id, "rejected")

    rejecter = f"@{query.from_user.username}" if query.from_user.username else str(query.from_user.id)
    suffix   = em(f"\n\n❌ <b>Rejected</b> by {rejecter}")
    try:
        if query.message.photo:
            await query.edit_message_caption((query.message.caption or "") + suffix, parse_mode="HTML")
        else:
            await query.edit_message_text((query.message.text or "") + suffix, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Could not edit appeal message: %s", exc)

    if appeal.get("telegram_id"):
        try:
            await context.bot.send_message(
                appeal["telegram_id"],
                em("❌ <b>Your appeal was reviewed and rejected.</b>\nYou remain on the scammer list."),
                parse_mode="HTML",
            )
        except TelegramError as exc:
            logger.debug("Could not notify user %s of appeal rejection: %s", appeal["telegram_id"], exc)


def register(app) -> None:
    app.add_handler(CommandHandler("appeal", appeal_command, filters=filters.ChatType.PRIVATE))
