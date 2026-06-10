"""Forward-message & /addid handler — works for both admins and regular users.

Admin forwards   → show sender info + [➕ Add as Scammer] button
Regular forwards → multi-step report: reason → ID → proof → admin DM

/addid <id> [reason]
  Admin   → direct add + kick + broadcast
  User    → fetches info, asks proof, sends to admin for approval

State is tracked manually via context.user_data (no ConversationHandler).
"""
from __future__ import annotations

import logging
import os

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, MessageHandler, CommandHandler, filters

from bot.services.admins import get_admin_ids as _admin_ids
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)

# ── user_data keys ────────────────────────────────────────────────────────────
_STATE  = "fwd_state"
_ID     = "fwd_id"
_UNAME  = "fwd_uname"
_NAME   = "fwd_name"
_REASON = "fwd_reason"

# States — forward report flow
S_REASON     = "reason"
S_ID         = "id"
S_PROOF      = "proof"
# State — /addid user flow (only proof needed, ID already known)
S_ADDID_PROOF = "addid_proof"


def _is_admin(uid: int) -> bool:
    return uid in _admin_ids()


def _extract_fwd_user(msg):
    """Extract (telegram_id, username, full_name) from a forwarded message.
    PTB v21 / Bot API 7+ — only forward_origin exists, no forward_from.
    Returns (None, None, None) for channels/hidden-user forwards.
    """
    if not msg.forward_origin:
        return None, None, None
    origin = msg.forward_origin
    sender = getattr(origin, "sender_user", None)
    if not sender:
        return None, None, None
    name = " ".join(filter(None, [sender.first_name, sender.last_name])) or "Unknown"
    return sender.id, sender.username, name


def _clear(ud: dict) -> None:
    for k in [_STATE, _ID, _UNAME, _NAME, _REASON]:
        ud.pop(k, None)


# ── /addid command — open to all ─────────────────────────────────────────────

async def on_addid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/addid <telegram_id> [reason]
    Admin  → direct add + kick + broadcast.
    User   → check duplicate, auto-fetch name, ask proof, submit report.
    """
    uid  = update.effective_user.id
    args = context.args

    if not args or not args[0].lstrip("@").isdigit():
        await update.message.reply_text(
            em(
                "📋 <b>Usage:</b> /addid &lt;telegram_id&gt; [reason]\n"
                "Example: <code>/addid 5886335494 Fraud seller</code>\n\n"
                "Bot will auto-fetch their username &amp; name from Telegram."
            ),
            parse_mode="HTML",
        )
        return

    telegram_id = int(args[0].lstrip("@"))
    reason      = " ".join(args[1:]).strip() if len(args) > 1 else "No reason provided"

    # Duplicate check
    from bot.db import scammer_exists
    dup = await scammer_exists(telegram_id, None)
    if dup:
        uname_d = f"@{dup.get('username')}" if dup.get("username") else "—"
        await update.message.reply_text(
            em(f"⚠️ <b>Already listed as #{dup['id']}</b> ({uname_d})."),
            parse_mode="HTML",
        )
        return

    # Auto-fetch info from Telegram
    username  = None
    full_name = "Unknown"
    fetched   = False
    try:
        chat      = await context.bot.get_chat(telegram_id)
        username  = chat.username
        full_name = " ".join(filter(None, [chat.first_name, chat.last_name])) or "Unknown"
        fetched   = True
    except Exception:
        pass

    uname_str  = f"@{username}" if username else "—"
    fetch_note = "✅ Details fetched from Telegram" if fetched else "⚠️ Could not fetch — tracked by ID only"

    # ── ADMIN: direct add + kick ──────────────────────────────────────────────
    if _is_admin(uid):
        from bot.db import add_scammer
        from bot.handlers.callbacks import _kick_from_all_groups
        from bot.services.broadcaster import broadcast_scammer

        scammer_id = await add_scammer(
            telegram_id = telegram_id,
            username    = username,
            name        = full_name,
            reason      = reason,
            proof       = None,
            added_by    = uid,
            severity    = "medium",
        )

        kicked   = await _kick_from_all_groups(context.bot, telegram_id)
        auto_ban = os.getenv("AUTO_BAN", "false").lower() in ("1", "true", "yes")
        action   = "🔨 Banned" if auto_ban else "🦵 Kicked"
        kick_line = f"\n{action} from <b>{kicked}</b> group(s)." if kicked else "\n⚠️ Not found in any active group."

        await broadcast_scammer(context.bot, scammer_id, username, telegram_id, reason, severity="medium")

        await update.message.reply_text(
            em(
                f"✅ <b>Scammer #{scammer_id} added!</b>\n\n"
                f"🔑 Telegram ID : <code>{telegram_id}</code>\n"
                f"📝 Username    : {uname_str}\n"
                f"👤 Name        : {full_name}\n"
                f"⚠️ Reason      : {reason}\n\n"
                f"{fetch_note}"
                f"{kick_line}"
            ),
            parse_mode="HTML",
        )
        return

    # ── NON-ADMIN ─────────────────────────────────────────────────────────────
    chat_type = update.effective_chat.type

    if chat_type in ("group", "supergroup"):
        # GROUP: one-shot report (no multi-step, groups don't support it cleanly)
        from bot.db import add_report
        report_id = await add_report(
            reporter_id      = uid,
            reporter_username= update.effective_user.username,
            target_id        = telegram_id,
            target_username  = username,
            target_full_name = full_name,
            reason           = reason,
            proof            = None,
            group_chat_id    = update.effective_chat.id,
            proof_file_id    = None,
        )
        await _notify_admins(context, report_id, telegram_id, username, full_name,
                             reason, update.effective_user, None, source="/addid group")
        await update.message.reply_text(
            em(
                f"✅ <b>Report #{report_id} submitted!</b>\n\n"
                f"🔑 ID: <code>{telegram_id}</code>  📝 {uname_str}\n"
                f"⚠️ Reason: {reason}\n\n"
                f"Admin will review shortly. 🙏"
            ),
            parse_mode="HTML",
        )
        return

    # PRIVATE: save state, ask for proof
    ud = context.user_data
    ud[_STATE]  = S_ADDID_PROOF
    ud[_ID]     = telegram_id
    ud[_UNAME]  = username
    ud[_NAME]   = full_name
    ud[_REASON] = reason

    await update.message.reply_text(
        em(
            f"🔍 <b>Reporting:</b>\n\n"
            f"🔑 Tele ID  : <code>{telegram_id}</code>\n"
            f"📝 Username : {uname_str}\n"
            f"👤 Name     : <b>{full_name}</b>\n"
            f"⚠️ Reason   : {reason}\n"
            f"ℹ️ {fetch_note}\n\n"
            f"📸 <b>Send proof</b> (screenshot/photo) or /skip\n"
            f"/cancel to abort"
        ),
        parse_mode="HTML",
    )


# ── Main entry: forwarded message received ────────────────────────────────────

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
                "If you know their Telegram ID:\n"
                "/addid &lt;id&gt; reason\n\n"
                "Or use /report to submit their @username."
            ),
            parse_mode="HTML",
        )
        return

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
        return

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
    elif state == S_ADDID_PROOF:
        await _finish_addid_report(update, context, proof_file_id=None)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if _is_admin(uid):
        return

    ud    = context.user_data
    state = ud.get(_STATE)
    if state not in (S_PROOF, S_ADDID_PROOF):
        return

    photo_id = update.message.photo[-1].file_id if update.message.photo else None
    if state == S_ADDID_PROOF:
        await _finish_addid_report(update, context, proof_file_id=photo_id)
    else:
        await _finish_report(update, context, proof_file_id=photo_id)


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if _is_admin(uid):
        return
    _clear(context.user_data)
    await update.message.reply_text(em("❌ Report cancelled."))


# ── Submit forward-report to admins ──────────────────────────────────────────

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

    await _notify_admins(context, report_id, fwd_id, fwd_uname, fwd_name, reason,
                         submitter, proof_file_id, source="message forward")
    _clear(context.user_data)
    await update.message.reply_text(
        em(f"✅ <b>Report #{report_id} submitted!</b>\nThank you for helping keep the community safe. 🙏"),
        parse_mode="HTML",
    )


async def _finish_addid_report(update, context, proof_file_id):
    """Complete /addid report flow for non-admin users."""
    from bot.db import add_report

    submitter = update.effective_user
    ud        = context.user_data
    tg_id     = ud.get(_ID)
    uname     = ud.get(_UNAME)
    name      = ud.get(_NAME, "Unknown")
    reason    = ud.get(_REASON, "No reason provided")

    report_id = await add_report(
        reporter_id      = submitter.id,
        reporter_username= submitter.username,
        target_id        = tg_id,
        target_username  = uname,
        target_full_name = name,
        reason           = reason,
        proof            = None,
        group_chat_id    = None,
        proof_file_id    = proof_file_id,
    )

    await _notify_admins(context, report_id, tg_id, uname, name, reason,
                         submitter, proof_file_id, source="/addid")
    _clear(context.user_data)
    await update.message.reply_text(
        em(f"✅ <b>Report #{report_id} submitted!</b>\nThank you for helping keep the community safe. 🙏"),
        parse_mode="HTML",
    )


async def _notify_admins(context, report_id, tg_id, uname, name, reason,
                         submitter, proof_file_id, source=""):
    uname_str    = f"@{uname}" if uname else "—"
    reporter_str = f"@{submitter.username}" if submitter.username else str(submitter.id)
    proof_line   = "\n📸 <b>Proof attached</b>" if proof_file_id else "\n❌ No proof"
    src_line     = f"\n<i>(via {source})</i>" if source else ""

    notif = em(
        f"📨 <b>Scammer Report #{report_id}</b>{src_line}\n\n"
        f"🎯 <b>Reported Person:</b>\n"
        f"  👤 Name     : <b>{name}</b>\n"
        f"  📝 Username : {uname_str}\n"
        f"  🔑 Tele ID  : <code>{tg_id or '—'}</code>\n\n"
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


# ── Group: forward check ─────────────────────────────────────────────────────

async def on_forward_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward in group → check DB, show result or suggest how to report."""
    msg = update.message
    uid = update.effective_user.id

    fwd_id, fwd_uname, fwd_name = _extract_fwd_user(msg)

    from bot.db import search_by_telegram_id, search_by_username

    if fwd_id:
        existing = await search_by_telegram_id(fwd_id)
        if not existing and fwd_uname:
            existing = await search_by_username(fwd_uname)

        if existing:
            e        = existing[0]
            sev      = (e.get("severity") or "medium").lower()
            sev_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(sev, "🟡")
            uname    = f"@{e['username']}" if e.get("username") else "—"
            await msg.reply_text(
                em(
                    f"🚨 <b>Known Scammer!</b>\n\n"
                    f"📝 Username : {uname}\n"
                    f"🔑 Tele ID  : <code>{e.get('telegram_id') or '—'}</code>\n"
                    f"{sev_icon} Severity  : {sev.capitalize()}\n"
                    f"⚠️ Reason   : {e['reason']}"
                ),
                parse_mode="HTML",
            )
        else:
            uname_str = f"@{fwd_uname}" if fwd_uname else "—"
            await msg.reply_text(
                em(
                    f"ℹ️ <b>Not in scammer list</b>\n\n"
                    f"👤 Name : <b>{fwd_name}</b>\n"
                    f"📝 Username : {uname_str}\n"
                    f"🔑 Tele ID : <code>{fwd_id}</code>\n\n"
                    f"To report: <code>/addid {fwd_id} &lt;reason&gt;</code>"
                ),
                parse_mode="HTML",
            )
    else:
        await msg.reply_text(
            em(
                "⚠️ <b>Could not identify the sender</b> (privacy enabled).\n\n"
                "If you know their username:\n"
                "<code>/add @username reason</code>\n\n"
                "If you know their Telegram ID:\n"
                "<code>/addid &lt;id&gt; reason</code>"
            ),
            parse_mode="HTML",
        )


# ── Group: /report @username [reason] ────────────────────────────────────────

async def on_report_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/report @username [reason]  OR  /report <telegram_id> [reason]  in a group."""
    msg  = update.message
    uid  = update.effective_user.id
    args = context.args

    if not args:
        await msg.reply_text(
            em(
                "📋 <b>Usage in group:</b>\n"
                "<code>/report @username reason</code>\n"
                "<code>/report &lt;telegram_id&gt; reason</code>\n\n"
                "For full report with proof, use bot PM."
            ),
            parse_mode="HTML",
        )
        return

    target = args[0].lstrip("@")
    reason = " ".join(args[1:]).strip() if len(args) > 1 else "No reason provided"

    tg_id    = int(target) if target.isdigit() else None
    username = None if target.isdigit() else target

    # Try to resolve from Telegram
    full_name = "Unknown"
    try:
        lookup = tg_id if tg_id else f"@{username}"
        chat   = await context.bot.get_chat(lookup)
        tg_id  = chat.id
        username = chat.username or username
        full_name = " ".join(filter(None, [chat.first_name, chat.last_name])) or "Unknown"
    except Exception:
        pass

    # Duplicate check
    from bot.db import scammer_exists
    dup = await scammer_exists(tg_id, username)
    if dup:
        uname_d = f"@{dup.get('username')}" if dup.get("username") else "—"
        await msg.reply_text(
            em(f"⚠️ <b>Already listed as #{dup['id']}</b> ({uname_d})."),
            parse_mode="HTML",
        )
        return

    from bot.db import add_report
    report_id = await add_report(
        reporter_id      = uid,
        reporter_username= update.effective_user.username,
        target_id        = tg_id,
        target_username  = username,
        target_full_name = full_name,
        reason           = reason,
        proof            = None,
        group_chat_id    = update.effective_chat.id,
        proof_file_id    = None,
    )

    await _notify_admins(context, report_id, tg_id, username, full_name,
                         reason, update.effective_user, None, source="/report group")

    uname_str = f"@{username}" if username else f"ID {tg_id or '—'}"
    await msg.reply_text(
        em(f"✅ <b>Report #{report_id} submitted!</b> ({uname_str})\nAdmin will review shortly. 🙏"),
        parse_mode="HTML",
    )


# ── Register handlers ─────────────────────────────────────────────────────────

def register(app) -> None:
    """Register all forward + /addid + group report handlers."""
    # /addid works in both PM and groups (different flow per chat type)
    app.add_handler(CommandHandler("addid", on_addid_command))

    # /report in GROUP (one-shot) — registered before build_report_handler (PM multi-step)
    app.add_handler(CommandHandler("report", on_report_group, filters=filters.ChatType.GROUPS))

    # Forward in GROUP → DB check / suggest
    app.add_handler(MessageHandler(filters.FORWARDED & filters.ChatType.GROUPS, on_forward_group))

    # Forward in PM → full multi-step flow
    app.add_handler(MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, on_forward))

    # PM conversation steps
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CommandHandler("skip",   on_skip))
    app.add_handler(CommandHandler("cancel", on_cancel))
