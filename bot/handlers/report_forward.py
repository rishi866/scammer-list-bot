"""Forward-message handler — works for both admins and regular users.

Admin forwards   → show sender info + [➕ Add as Scammer] button
Regular forwards → multi-step report: reason → ID → proof → admin DM

State is tracked manually via context.user_data (no ConversationHandler needed).
"""
from __future__ import annotations

import logging
import os

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, MessageHandler, CommandHandler, filters

from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)

# user_data keys
_STATE  = "fwd_state"
_ID     = "fwd_id"
_UNAME  = "fwd_uname"
_NAME   = "fwd_name"
_REASON = "fwd_reason"

# States
S_REASON = "reason"
S_ID     = "id"
S_PROOF  = "proof"


def _admin_ids() -> set[int]:
    return {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}


def _is_admin(uid: int) -> bool:
    return uid in _admin_ids()


def _extract_fwd_user(msg):
    orig = None
    if msg.forward_origin and hasattr(msg.forward_origin, "sender_user"):
        orig = msg.forward_origin.sender_user
    if not orig and msg.forward_from:
        orig = msg.forward_from
    if not orig:
        return None, None, None
    name = " ".join(filter(None, [orig.first_name, orig.last_name])) or "Unknown"
    return orig.id, orig.username, name


def _clear(ud: dict) -> None:
    for k in [_STATE, _ID, _UNAME, _NAME, _REASON]:
        ud.pop(k, None)


# ── Main entry: forwarded message received ───────────────────────────────────

async def on_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    uid = update.effective_user.id
    ud  = context.user_data

    fwd_id, fwd_uname, fwd_name = _extract_fwd_user(msg)

    # ── ADMIN FLOW ──────────────────────────────────────────────────────────
    if _is_admin(uid):
        if not fwd_id:
            await msg.reply_text(
                em(
                    "⚠️ <b>Could not identify the sender.</b>\n\n"
                    "Their <b>Forward Privacy</b> is enabled — Telegram hides their identity.\n\n"
                    "<b>To add them manually:</b>\n"
                    "• Forward their message to @userinfobot → get their ID\n"
                    "• Then: /addid &lt;id&gt; &lt;reason&gt;\n\n"
                    "Or if you know their @username:\n"
                    "/addid @username reason"
                ),
                parse_mode="HTML",
            )
            return

        from bot.db import search_by_telegram_id, search_by_username, update_scammer_telegram_id
        existing = await search_by_telegram_id(fwd_id)
        if not existing and fwd_uname:
            existing = await search_by_username(fwd_uname)

        uname_str = f"@{fwd_uname}" if fwd_uname else "—"

        if existing:
            e = existing[0]
            # Update ID if missing
            if not e.get("telegram_id"):
                await update_scammer_telegram_id(e["id"], fwd_id, fwd_uname)
                await msg.reply_text(
                    em(f"✅ Telegram ID saved for Scammer #{e['id']} ({uname_str}): <code>{fwd_id}</code>"),
                    parse_mode="HTML",
                )
            else:
                await msg.reply_text(
                    em(
                        f"ℹ️ <b>Already listed as Scammer #{e['id']}</b>\n"
                        f"📝 Username : {uname_str}\n"
                        f"🔑 Tele ID  : <code>{fwd_id}</code>\n"
                        f"⚠️ Reason   : {e['reason']}"
                    ),
                    parse_mode="HTML",
                )
        else:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Add as Scammer", callback_data=f"quickadd:{fwd_id}:{fwd_uname or ''}")
            ]])
            await msg.reply_text(
                em(
                    f"ℹ️ <b>Not in scammer list</b>\n\n"
                    f"👤 Name     : <b>{fwd_name}</b>\n"
                    f"📝 Username : {uname_str}\n"
                    f"🔑 Tele ID  : <code>{fwd_id}</code>\n\n"
                    f"Want to add them as a scammer?"
                ),
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        return

    # ── NON-ADMIN FLOW ──────────────────────────────────────────────────────

    if not fwd_id:
        await msg.reply_text(
            em(
                "⚠️ <b>Could not identify the sender.</b>\n\n"
                "Their <b>Forward Privacy</b> is enabled.\n\n"
                "Please use /report and enter their @username manually."
            ),
            parse_mode="HTML",
        )
        return

    # Check if already listed
    from bot.db import search_by_telegram_id, search_by_username
    existing = await search_by_telegram_id(fwd_id)
    if not existing and fwd_uname:
        existing = await search_by_username(fwd_uname)

    if existing:
        e        = existing[0]
        sev      = (e.get("severity") or "medium").lower()
        sev_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(sev, "🟡")
        uname    = f"@{e['username']}" if e.get("username") else "—"
        history  = [u for u in (e.get("username_history") or []) if u]
        hist_str = ", ".join(f"@{u}" for u in history) if history else "—"
        await msg.reply_text(
            em(
                f"🚨 <b>Already a confirmed scammer!</b>\n\n"
                f"📋 Scammer #{e['id']}\n"
                f"📝 Username : {uname}\n"
                f"🔑 Tele ID  : <code>{e.get('telegram_id') or '—'}</code>\n"
                f"{sev_icon} Severity  : {sev.capitalize()}\n"
                f"⚠️ Reason   : {e['reason']}\n"
                f"🔄 Past usernames: {hist_str}"
            ),
            parse_mode="HTML",
        )
        return

    # Start report flow
    ud[_STATE] = S_REASON
    ud[_ID]    = fwd_id
    ud[_UNAME] = fwd_uname
    ud[_NAME]  = fwd_name

    uname_str = f"@{fwd_uname}" if fwd_uname else "—"
    await msg.reply_text(
        em(
            f"📨 <b>Report Submission</b>\n\n"
            f"Reporting:\n"
            f"  👤 Name     : <b>{fwd_name}</b>\n"
            f"  📝 Username : {uname_str}\n"
            f"  🔑 Tele ID  : <code>{fwd_id}</code>\n\n"
            f"⚠️ <b>What did this person do?</b> Describe briefly:\n"
            f"(/cancel to abort)"
        ),
        parse_mode="HTML",
    )


# ── Non-admin text message handler (conversation steps) ──────────────────────

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if _is_admin(uid):
        return  # admins not in this flow

    ud    = context.user_data
    state = ud.get(_STATE)
    if not state:
        return

    text = update.message.text.strip()

    if state == S_REASON:
        if len(text) < 5:
            await update.message.reply_text("⚠️ Please give more detail (min 5 chars).")
            return
        ud[_REASON] = text
        ud[_STATE]  = S_ID
        existing_id = ud.get(_ID)
        await update.message.reply_text(
            em(
                f"🔑 Telegram ID already detected: <code>{existing_id}</code>\n\n"
                f"If you know a different/correct ID, send it now.\n"
                f"Otherwise send /skip to continue.\n\n"
                f"Or send their @username if ID is unknown."
            ),
            parse_mode="HTML",
        )

    elif state == S_ID:
        if text.lstrip("@").isdigit():
            ud[_ID] = int(text.lstrip("@"))
        elif text.startswith("@"):
            ud[_UNAME] = text.lstrip("@")
        ud[_STATE] = S_PROOF
        await update.message.reply_text(
            em("📸 <b>Send proof</b> (screenshot/photo). No proof? Send /skip"),
            parse_mode="HTML",
        )

    elif state == S_PROOF:
        await _finish_report(update, context, proof_file_id=None)


async def on_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if _is_admin(uid):
        return

    ud    = context.user_data
    state = ud.get(_STATE)
    if not state:
        return

    if state == S_ID:
        ud[_STATE] = S_PROOF
        await update.message.reply_text(
            em("📸 <b>Send proof</b> (screenshot/photo). No proof? Send /skip"),
            parse_mode="HTML",
        )
    elif state == S_PROOF:
        await _finish_report(update, context, proof_file_id=None)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if _is_admin(uid):
        return

    ud    = context.user_data
    state = ud.get(_STATE)
    if state != S_PROOF:
        return

    photo_id = update.message.photo[-1].file_id if update.message.photo else None
    await _finish_report(update, context, proof_file_id=photo_id)


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if _is_admin(uid):
        return
    _clear(context.user_data)
    await update.message.reply_text(em("❌ Report cancelled."))


# ── Submit report to admins ───────────────────────────────────────────────────

async def _finish_report(update, context, proof_file_id):
    from bot.db import add_report

    submitter = update.effective_user
    ud        = context.user_data
    fwd_id    = ud.get(_ID)
    fwd_uname = ud.get(_UNAME)
    fwd_name  = ud.get(_NAME, "Unknown")
    reason    = ud.get(_REASON, "No reason provided")

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
    proof_line   = "\n📸 <b>Proof attached</b>" if proof_file_id else "\n❌ No proof"

    notif = em(
        f"📨 <b>Scammer Report #{report_id}</b>\n"
        f"<i>(via message forward)</i>\n\n"
        f"🎯 <b>Reported Person:</b>\n"
        f"  👤 Name     : <b>{fwd_name}</b>\n"
        f"  📝 Username : {uname_str}\n"
        f"  🔑 Tele ID  : <code>{fwd_id or '—'}</code>\n\n"
        f"⚠️ <b>Reason:</b> {reason}{proof_line}\n\n"
        f"📤 <b>Reporter:</b> {reporter_str} (ID: <code>{submitter.id}</code>)\n\n"
        f"👇 Approve with severity or ignore:"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 High",   callback_data=f"approve_high:{report_id}"),
            InlineKeyboardButton("🟡 Medium", callback_data=f"approve_medium:{report_id}"),
            InlineKeyboardButton("🟢 Low",    callback_data=f"approve_low:{report_id}"),
        ],
        [InlineKeyboardButton("❌ Ignore", callback_data=f"reject_sub:{report_id}")],
    ])

    for aid in _admin_ids():
        try:
            if proof_file_id:
                await context.bot.send_photo(aid, photo=proof_file_id, caption=notif,
                                              parse_mode="HTML", reply_markup=keyboard)
            else:
                await context.bot.send_message(aid, notif, parse_mode="HTML", reply_markup=keyboard)
        except Exception as exc:
            logger.warning("Could not notify admin %s: %s", aid, exc)

    _clear(context.user_data)
    await update.message.reply_text(
        em(f"✅ <b>Report #{report_id} submitted!</b>\nThank you for helping keep the community safe. 🙏"),
        parse_mode="HTML",
    )


# ── Register handlers ─────────────────────────────────────────────────────────

def register(app) -> None:
    """Register all forward-related handlers on the Application."""
    app.add_handler(MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, on_forward))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CommandHandler("skip",   on_skip))
    app.add_handler(CommandHandler("cancel", on_cancel))
