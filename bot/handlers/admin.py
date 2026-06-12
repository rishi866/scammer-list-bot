"""Admin-only commands: /add /remove /list /pending /approve /reject /stats."""
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Callable

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters

from telegram.error import TelegramError

from bot.db import (
    add_scammer,
    remove_scammer,
    get_scammer_by_id,
    get_scammer_by_seq,
    list_scammers,
    count_scammers,
    list_pending_reports,
    get_report,
    update_report_status,
    count_reports,
    get_scammers_missing_id,
    update_scammer_telegram_id,
    update_scammer_field,
    update_scammer_username,
    touch_username_check,
    EDITABLE_FIELDS,
    scammer_exists,
    search_by_telegram_id,
    search_by_username,
)
from bot.services.admins import (
    get_admin_ids as _get_admin_ids,
    get_admin_ids as _admin_ids,
    resolve_protected_role,
    protected_block_message,
)
from bot.services.audit import audit
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)

ADD_TARGET, ADD_NAME, ADD_REASON, ADD_PROOF, ADD_PAYMENT, ADD_NOTES = range(6)


def admin_only(func: Callable):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in _get_admin_ids():
            await update.message.reply_text(em("⛔ Admins only."), parse_mode="HTML")
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


# ── /add flow (private chat, direct DB insert) ────────────────────────────────

@admin_only
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        em(
            "➕ <b>Add Scammer</b> — Step 1/6\n\n"
            "Send their <b>@username</b> or <b>Telegram ID</b>.\n"
            "Type <b>none</b> if unknown.\n"
            "/cancel to abort."
        ),
        parse_mode="HTML",
    )
    return ADD_TARGET


async def add_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from telegram.error import TelegramError
    raw = update.message.text.strip()

    if raw.lower() == "none":
        context.user_data["add_target_id"]    = None
        context.user_data["add_target_uname"] = None
        context.user_data["add_target_name"]  = None
    else:
        # Try to resolve via Telegram — works for both @username and numeric ID
        lookup = int(raw.lstrip("@")) if raw.lstrip("@").isdigit() else f"@{raw.lstrip('@')}"
        try:
            chat = await context.bot.get_chat(lookup)
            context.user_data["add_target_id"]    = chat.id
            context.user_data["add_target_uname"] = chat.username
            full_name = " ".join(filter(None, [chat.first_name, chat.last_name])) or None
            context.user_data["add_target_name"]  = full_name

            # Show what was fetched
            uname = f"@{chat.username}" if chat.username else "—"
            await update.message.reply_text(
                em(
                    f"✅ <b>Resolved from Telegram:</b>\n"
                    f"  👤 Name: <b>{full_name or '—'}</b>\n"
                    f"  📝 Username: {uname}\n"
                    f"  🔑 ID: <code>{chat.id}</code>\n\n"
                    f"👤 Step 2/6 — Confirm or change their full name\n"
                    f"(Send <b>auto</b> to keep: <b>{full_name or '—'}</b>):"
                ),
                parse_mode="HTML",
            )
        except TelegramError as e:
            # Could not resolve — store what was given manually
            if raw.lstrip("@").isdigit():
                context.user_data["add_target_id"]    = int(raw.lstrip("@"))
                context.user_data["add_target_uname"] = None
            else:
                context.user_data["add_target_id"]    = None
                context.user_data["add_target_uname"] = raw.lstrip("@")
            context.user_data["add_target_name"] = None
            await update.message.reply_text(
                em(f"⚠️ Could not fetch from Telegram (<code>{e}</code>).\n\n"
                   f"👤 Step 2/6 — Enter their full name manually:"),
                parse_mode="HTML",
            )

    # Protect bot owner/admins from being added as a scammer
    role = await resolve_protected_role(
        context.user_data.get("add_target_id"),
        context.user_data.get("add_target_uname"),
        bot=context.bot,
    )
    if role:
        await update.message.reply_text(em(protected_block_message(role)), parse_mode="HTML")
        context.user_data.clear()
        return ConversationHandler.END

    return ADD_NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    # "auto" = keep the Telegram-fetched name
    if raw.lower() == "auto" and context.user_data.get("add_target_name"):
        context.user_data["add_name"] = context.user_data["add_target_name"]
    else:
        context.user_data["add_name"] = raw
    await update.message.reply_text(em("⚠️ Step 3/6 — Reason for listing:"), parse_mode="HTML")
    return ADD_REASON


async def add_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["add_reason"] = update.message.text.strip()
    await update.message.reply_text(
        em("🔗 Step 4/6 — Proof link or description (or <b>none</b>):"),
        parse_mode="HTML",
    )
    return ADD_PROOF


async def add_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    context.user_data["add_proof"] = None if raw.lower() == "none" else raw
    await update.message.reply_text(
        em(
            "💳 Step 5/6 — Payment info (Binance ID / UPI / wallet address) they used,\n"
            "or <b>none</b> if not applicable:"
        ),
        parse_mode="HTML",
    )
    return ADD_PAYMENT


async def add_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    context.user_data["add_payment"] = None if raw.lower() == "none" else raw
    await update.message.reply_text(
        em("📝 Step 6/6 — Additional notes (or <b>none</b>):"),
        parse_mode="HTML",
    )
    return ADD_NOTES


async def add_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw   = update.message.text.strip()
    notes = None if raw.lower() == "none" else raw

    scammer_id = await add_scammer(
        telegram_id  = context.user_data.get("add_target_id"),
        username     = context.user_data.get("add_target_uname"),
        name         = context.user_data["add_name"],
        reason       = context.user_data["add_reason"],
        proof        = context.user_data.get("add_proof"),
        added_by     = update.effective_user.id,
        notes        = notes,
        payment_info = context.user_data.get("add_payment"),
    )
    await update.message.reply_text(
        em(f"✅ Scammer added as <b>#{scammer_id}</b>."),
        parse_mode="HTML",
    )
    context.user_data.clear()
    return ConversationHandler.END


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(em("❌ Cancelled."), parse_mode="HTML")
    return ConversationHandler.END


def build_add_handler() -> ConversationHandler:
    """Multi-step /add for admins in PRIVATE chat (direct DB insert, no approval)."""
    return ConversationHandler(
        entry_points=[CommandHandler("add", add_start, filters=filters.ChatType.PRIVATE)],
        states={
            ADD_TARGET:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_target)],
            ADD_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            ADD_REASON:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_reason)],
            ADD_PROOF:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_proof)],
            ADD_PAYMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_payment)],
            ADD_NOTES:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_notes)],
        },
        fallbacks=[CommandHandler("cancel", add_cancel)],
    )


# ── /remove ───────────────────────────────────────────────────────────────────

@admin_only
async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /remove <number>  (use number from /scammer_list)")
        return

    seq = int(args[0])

    # Look up by sequential position (as shown in /scammer_list)
    from bot.db import get_scammer_by_seq
    entry = await get_scammer_by_seq(seq)

    if not entry:
        await update.message.reply_text(em(f"❌ No scammer at position #{seq}."), parse_mode="HTML")
        return

    ok = await remove_scammer(entry["id"])
    if ok:
        uname = f"@{entry['username']}" if entry.get("username") else f"ID {entry.get('telegram_id') or '—'}"
        await update.message.reply_text(
            em(f"✅ <b>#{seq} ({uname}) removed</b> from scammer list."),
            parse_mode="HTML",
        )
        await audit(update.effective_user, "remove", "scammer", entry["id"], f"{uname}")
    else:
        await update.message.reply_text(em(f"❌ Could not remove #{seq}."), parse_mode="HTML")


# ── /edit ─────────────────────────────────────────────────────────────────────

_EDIT_USAGE = (
    "✏️ <b>Usage:</b> /edit &lt;#&gt; &lt;field&gt; &lt;value&gt;\n"
    "(use the <b>#</b> shown in /scammer_list)\n\n"
    "<b>Fields:</b> reason · severity · username · name · id · notes · proof · payment\n\n"
    "<b>Examples:</b>\n"
    "<code>/edit 3 reason Fake crypto investment scheme</code>\n"
    "<code>/edit 3 severity high</code>\n"
    "<code>/edit 3 username new_username</code>\n"
    "<code>/edit 3 id 123456789</code>\n"
    "<code>/edit 3 payment binanceid123</code>"
)


@admin_only
async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 3 or not args[0].isdigit():
        await update.message.reply_text(em(_EDIT_USAGE), parse_mode="HTML")
        return

    seq       = int(args[0])
    field     = args[1].lower()
    value_raw = " ".join(args[2:]).strip()

    if field not in EDITABLE_FIELDS:
        await update.message.reply_text(em(_EDIT_USAGE), parse_mode="HTML")
        return

    entry = await get_scammer_by_seq(seq)
    if not entry:
        await update.message.reply_text(em(f"❌ No scammer at position #{seq}."), parse_mode="HTML")
        return

    # Per-field validation / normalization
    if field in ("id", "telegram_id"):
        if not value_raw.lstrip("-").isdigit():
            await update.message.reply_text(em("⚠️ Telegram ID must be a number."), parse_mode="HTML")
            return
        value: object = int(value_raw)
    elif field == "severity":
        value = value_raw.lower()
        if value not in ("high", "medium", "low"):
            await update.message.reply_text(em("⚠️ Severity must be: high, medium, or low."), parse_mode="HTML")
            return
    elif field == "username":
        value = value_raw.lstrip("@") or None
    else:
        value = value_raw

    column    = EDITABLE_FIELDS[field]
    old_value = entry.get(column)

    ok = await update_scammer_field(entry["id"], field, value)
    if not ok:
        await update.message.reply_text(em(f"❌ Could not update #{seq}."), parse_mode="HTML")
        return

    uname = f"@{entry.get('username')}" if entry.get("username") else f"ID {entry.get('telegram_id') or '—'}"
    await update.message.reply_text(
        em(
            f"✅ <b>#{seq} ({uname}) updated</b>\n\n"
            f"Field : <b>{field}</b>\n"
            f"Old   : <code>{old_value if old_value not in (None, '') else '—'}</code>\n"
            f"New   : <code>{value}</code>"
        ),
        parse_mode="HTML",
    )
    await audit(update.effective_user, "edit", "scammer", entry["id"],
                f"{field}: {old_value if old_value not in (None, '') else '—'} → {value}")


# ── /refreshusername ─────────────────────────────────────────────────────────

_REFRESH_USAGE = (
    "🔄 <b>Usage:</b> /refreshusername &lt;#&gt;\n"
    "(use the <b>#</b> shown in /scammer_list)\n\n"
    "Re-checks this scammer's current username &amp; name on Telegram right "
    "now — instead of waiting for the automatic 6-hour refresh."
)


@admin_only
async def refreshusername_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(em(_REFRESH_USAGE), parse_mode="HTML")
        return

    seq   = int(args[0])
    entry = await get_scammer_by_seq(seq)
    if not entry:
        await update.message.reply_text(em(f"❌ No scammer at position #{seq}."), parse_mode="HTML")
        return

    tid = entry.get("telegram_id")
    if not tid:
        await update.message.reply_text(
            em(f"⚠️ #{seq} has no Telegram ID on file — nothing to refresh.\nUse <code>/edit {seq} id &lt;telegram_id&gt;</code> first."),
            parse_mode="HTML",
        )
        return

    chat = None
    err  = None
    try:
        chat = await context.bot.get_chat(tid)
    except TelegramError as e:
        err = e

    # Fallback: get_chat(id) needs an "access hash" the bot often loses once
    # a scammer is kicked/banned. get_chat("@username") is a *global*
    # username-directory lookup and can succeed even then.
    reassign_note = ""
    if chat is None and entry.get("username"):
        old_uname = entry["username"]
        try:
            by_uname = await context.bot.get_chat(f"@{old_uname}")
            if by_uname.id == tid:
                chat = by_uname  # same person — just refresh via @username
            else:
                reassign_note = (
                    f"\n\n⚠️ <b>Heads up:</b> @{old_uname} ab kisi aur account "
                    f"(ID <code>{by_uname.id}</code>) ka hai — is scammer ne shayad "
                    f"username badal liya hai. Naya username pata chale to "
                    f"<code>/edit {seq} username &lt;naya_username&gt;</code> chalana."
                )
        except TelegramError:
            pass  # neither lookup worked — fall through to cache below

    if chat is None:
        # Final fallback: check the passive "seen users" cache (built from
        # group messages/joins) — bot may know this person even if get_chat()
        # can't reach them right now.
        from bot.db import get_bot_user
        cached  = await get_bot_user(tid)
        hint    = ""
        if cached and (cached.get("username") or cached.get("full_name")):
            cu   = f"@{cached['username']}" if cached.get("username") else "—"
            cn   = cached.get("full_name") or "—"
            past = [u for u in (cached.get("username_history") or []) if u]
            ph   = ("\n   Past    : " + ", ".join(f"@{u}" for u in past)) if past else ""
            hint = (
                f"\n\n📦 <b>But found in seen-users cache:</b>\n"
                f"   Username: {cu}\n"
                f"   Name    : {cn}"
                f"{ph}\n\n"
                f"<code>/edit {seq} username {cached.get('username') or ''}</code>\n"
                f"<code>/edit {seq} name {cn}</code>"
            )
        await update.message.reply_text(
            em(
                f"⚠️ <b>Could not refresh #{seq}</b>\n\n"
                f"Telegram says: <code>{err}</code>\n\n"
                "This usually means the bot has no access to this user "
                "(they've never /start'd the bot and haven't been seen "
                "joining a group the bot is in)."
                f"{hint}{reassign_note}"
                + ("" if (hint or reassign_note) else "\n\nUse /edit to set "
                   "the username/name manually instead — it'll auto-sync "
                   "the moment the bot does see them.")
            ),
            parse_mode="HTML",
        )
        return

    old_username = entry.get("username")
    old_name     = entry.get("name")
    new_username = chat.username
    new_name     = " ".join(filter(None, [chat.first_name, chat.last_name])) or None

    changed = []
    if new_username != old_username:
        await update_scammer_username(entry["id"], new_username, old_username)
        changed.append(
            f"📝 Username : {f'@{old_username}' if old_username else '—'} → "
            f"{f'@{new_username}' if new_username else '—'}"
        )
    else:
        await touch_username_check(entry["id"])

    if new_name and new_name != old_name:
        await update_scammer_field(entry["id"], "name", new_name)
        changed.append(f"👤 Name     : {old_name or '—'} → {new_name}")

    if changed:
        await update.message.reply_text(
            em(f"✅ <b>#{seq} refreshed from Telegram!</b>\n\n" + "\n".join(changed) + reassign_note),
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            em(f"ℹ️ #{seq} — already up to date (no changes on Telegram)." + reassign_note),
            parse_mode="HTML",
        )


# ── /list ─────────────────────────────────────────────────────────────────────

@admin_only
async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        page = int((context.args or ["1"])[0]) - 1
    except ValueError:
        page = 0
    per_page = 10
    entries  = await list_scammers(limit=per_page, offset=page * per_page)
    total    = await count_scammers()

    if not entries:
        await update.message.reply_text(em("📋 No scammers in the list yet."), parse_mode="HTML")
        return

    lines = []
    for e in entries:
        uname = f"@{e['username']}" if e.get("username") else "—"
        tid   = str(e["telegram_id"]) if e.get("telegram_id") else "—"
        lines.append(f"<b>#{e['id']}</b> {e.get('name','?')} | {uname} | {tid} | {e['reason'][:40]}")

    await update.message.reply_text(
        em(f"📋 <b>Scammer List</b> (page {page+1}, total {total})\n\n")
        + "\n".join(lines)
        + f"\n\n/list {page+2} → next page",
        parse_mode="HTML",
    )


# ── /pending ──────────────────────────────────────────────────────────────────

@admin_only
async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reports = await list_pending_reports()
    if not reports:
        await update.message.reply_text(em("✅ No pending reports."), parse_mode="HTML")
        return

    lines = []
    for r in reports[:15]:
        target = f"@{r['target_username']}" if r.get("target_username") else str(r.get("target_id") or "?")
        name   = r.get("target_full_name") or "—"
        lines.append(
            f"<b>#{r['id']}</b>  {target}  |  {name}\n"
            f"  Reason : {r['reason'][:60]}\n"
            f"  Proof  : {(r.get('proof') or '—')[:60]}\n"
            f"  From   : @{r.get('reporter_username') or r['reporter_id']}\n"
            f"  /approve {r['id']}  |  /reject {r['id']}"
        )

    await update.message.reply_text(
        em(f"📨 <b>Pending Reports ({len(reports)})</b>\n\n") + "\n\n".join(lines),
        parse_mode="HTML",
    )


# ── /approve /reject ──────────────────────────────────────────────────────────

@admin_only
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /approve <report_id>")
        return
    rid    = int(args[0])
    report = await get_report(rid)
    if not report:
        await update.message.reply_text(em(f"❌ Report #{rid} not found."), parse_mode="HTML")
        return
    if report["status"] != "pending":
        await update.message.reply_text(em(f"⚠️ Report #{rid} is already {report['status']}."), parse_mode="HTML")
        return

    scammer_id = await add_scammer(
        telegram_id  = report.get("target_id"),
        username     = report.get("target_username"),
        name         = report.get("target_full_name") or report.get("target_username") or str(report.get("target_id") or "Unknown"),
        reason       = report["reason"],
        proof        = report.get("proof"),
        added_by     = update.effective_user.id,
        payment_info = report.get("payment_info"),
    )
    await update_report_status(rid, "approved")
    await update.message.reply_text(
        em(f"✅ Report #{rid} approved — added as <b>#{scammer_id}</b>."),
        parse_mode="HTML",
    )

    from bot.handlers.callbacks import _broadcast_resolution, _target_str
    approver = f"@{update.effective_user.username}" if update.effective_user.username else str(update.effective_user.id)
    await _broadcast_resolution(
        context,
        actor=approver,
        actor_id=update.effective_user.id,
        headline=f"✅ <b>Submission #{rid} approved</b>\n🎯 {_target_str(report)} → Scammer #{scammer_id}",
    )
    await audit(update.effective_user, "approve", "scammer", scammer_id, f"report#{rid} (via /approve)")


@admin_only
async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /reject <report_id>")
        return
    rid    = int(args[0])
    report = await get_report(rid)
    if not report:
        await update.message.reply_text(em(f"❌ Report #{rid} not found."), parse_mode="HTML")
        return
    await update_report_status(rid, "rejected")
    await update.message.reply_text(em(f"❌ Report #{rid} rejected."), parse_mode="HTML")

    from bot.handlers.callbacks import _broadcast_resolution, _target_str
    rejecter = f"@{update.effective_user.username}" if update.effective_user.username else str(update.effective_user.id)
    await _broadcast_resolution(
        context,
        actor=rejecter,
        actor_id=update.effective_user.id,
        headline=f"❌ <b>Submission #{rid} rejected</b>\n🎯 {_target_str(report)}",
    )
    await audit(update.effective_user, "reject", "report", rid, "via /reject")


# ── /stats ────────────────────────────────────────────────────────────────────

@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    total    = await count_scammers()
    pending  = await count_reports("pending")
    approved = await count_reports("approved")
    rejected = await count_reports("rejected")

    await update.message.reply_text(
        em(
            f"📊 <b>Stats</b>\n\n"
            f"🔴 Scammers listed : <b>{total}</b>\n"
            f"📨 Reports pending : <b>{pending}</b>\n"
            f"✅ Reports approved: <b>{approved}</b>\n"
            f"❌ Reports rejected: <b>{rejected}</b>"
        ),
        parse_mode="HTML",
    )


# ── Forward message → auto-update ID ─────────────────────────────────────────

async def handle_forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """When admin forwards any message from a scammer → auto-resolve their ID.

    Works as long as the original sender hasn't hidden their identity in
    Telegram's privacy settings (Forward Messages privacy).
    """
    if update.effective_user.id not in _admin_ids():
        return

    msg = update.message
    if not msg:
        return

    # Extract original sender from forward info (PTB v20+ uses forward_origin)
    orig_user = None

    # New API (PTB v21 / Bot API 7+)
    if msg.forward_origin:
        origin = msg.forward_origin
        # MessageOriginUser has .sender_user
        if hasattr(origin, "sender_user") and origin.sender_user:
            orig_user = origin.sender_user
    # Legacy fallback
    if not orig_user and msg.forward_from:
        orig_user = msg.forward_from

    if not orig_user:
        # Privacy settings hide the sender — tell admin what to do
        await msg.reply_text(
            em(
                "⚠️ <b>Could not identify the sender.</b>\n\n"
                "Their <b>Forward Privacy</b> is enabled — Telegram hides their identity.\n\n"
                "<b>How to get their ID:</b>\n"
                "1️⃣ Forward their message to @userinfobot → it will show their ID\n"
                "2️⃣ Then use: /addid &lt;telegram_id&gt; &lt;reason&gt;\n\n"
                "Or if you know their @username:\n"
                "/addid @username reason"
            ),
            parse_mode="HTML",
        )
        return

    fwd_id    = orig_user.id
    fwd_uname = orig_user.username

    # Search DB by ID or username
    from bot.db import search_by_telegram_id, search_by_username
    results = await search_by_telegram_id(fwd_id)
    if not results and fwd_uname:
        results = await search_by_username(fwd_uname)

    if not results:
        # Not in DB — show their info and offer quick-add button
        uname    = f"@{fwd_uname}" if fwd_uname else "—"
        fname    = " ".join(filter(None, [orig_user.first_name, orig_user.last_name])) or "—"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "➕ Add as Scammer",
                callback_data=f"quickadd:{fwd_id}:{fwd_uname or ''}",
            )
        ]])
        await msg.reply_text(
            em(
                f"ℹ️ <b>User not in scammer list</b>\n\n"
                f"👤 Name     : <b>{fname}</b>\n"
                f"📝 Username : {uname}\n"
                f"🔑 Tele ID  : <code>{fwd_id}</code>\n\n"
                f"Want to add them as a scammer?"
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    updated = []
    for entry in results:
        if not entry.get("telegram_id"):
            await update_scammer_telegram_id(entry["id"], fwd_id, fwd_uname or entry.get("username"))
            updated.append(entry)

    if not updated:
        # Already had ID — just confirm
        uname = f"@{results[0].get('username')}" if results[0].get("username") else "—"
        await msg.reply_text(
            em(f"ℹ️ Scammer #{results[0]['id']} ({uname}) already has ID <code>{fwd_id}</code>."),
            parse_mode="HTML",
        )
        return

    lines = []
    for e in updated:
        uname = f"@{fwd_uname or e.get('username')}" if (fwd_uname or e.get("username")) else "—"
        lines.append(f"✅ #{e['id']} {uname} → ID <code>{fwd_id}</code>")

    await msg.reply_text(
        em(f"🎯 <b>Scammer ID auto-updated!</b>\n\n" + "\n".join(lines)),
        parse_mode="HTML",
    )


# ── /addid ────────────────────────────────────────────────────────────────────

@admin_only
async def addid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/addid <telegram_id> [reason]
    Add a scammer by Telegram ID only — username & name auto-fetched.
    Example: /addid 5886335494 Fraud seller
    """
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            em(
                "Usage: /addid <telegram_id> [reason]\n"
                "Example: /addid 5886335494 Fraud seller\n\n"
                "Bot will auto-fetch their current username & name."
            ),
        )
        return

    telegram_id = int(args[0])
    reason      = " ".join(args[1:]).strip() if len(args) > 1 else "No reason provided"

    # Check duplicate
    dup = await scammer_exists(telegram_id, None)
    if dup:
        uname_d = f"@{dup.get('username')}" if dup.get("username") else "—"
        await update.message.reply_text(
            em(f"⚠️ Already listed as <b>#{dup['id']}</b> ({uname_d})."),
            parse_mode="HTML",
        )
        return

    # Try to fetch current info from Telegram
    username  = None
    full_name = "Unknown"
    fetched   = False

    try:
        chat      = await context.bot.get_chat(telegram_id)
        username  = chat.username
        full_name = " ".join(filter(None, [chat.first_name, chat.last_name])) or "Unknown"
        fetched   = True
    except TelegramError as e:
        logger.info("get_chat(%s) failed (will track by ID only): %s", telegram_id, e)

    scammer_id = await add_scammer(
        telegram_id = telegram_id,
        username    = username,
        name        = full_name,
        reason      = reason,
        proof       = None,
        added_by    = update.effective_user.id,
        severity    = "medium",
    )

    # Kick from all groups immediately
    from bot.handlers.callbacks import _kick_from_all_groups
    from bot.services.broadcaster import broadcast_scammer
    import os as _os

    kicked = await _kick_from_all_groups(
        context.bot, telegram_id,
        username=username,
        reason=reason,
        scammer_id=scammer_id,
    )
    auto_ban = _os.getenv("AUTO_BAN", "false").lower() in ("1", "true", "yes")
    action   = "🔨 Banned" if auto_ban else "🦵 Kicked"
    kick_line = f"\n{action} from <b>{kicked}</b> group(s)." if kicked else "\n⚠️ Not found in any active group."

    await broadcast_scammer(context.bot, scammer_id, username, telegram_id, reason, severity="medium")

    uname_str  = f"@{username}" if username else "— (will update when bot sees them)"
    fetch_note = "✅ Fetched from Telegram" if fetched else "⚠️ Could not fetch — will auto-update later"

    await update.message.reply_text(
        em(
            f"✅ <b>Scammer #{scammer_id} added!</b>\n\n"
            f"🔑 Telegram ID : <code>{telegram_id}</code>\n"
            f"📝 Username    : {uname_str}\n"
            f"👤 Name        : {full_name}\n"
            f"⚠️ Reason      : {reason}\n\n"
            f"{fetch_note}\n"
            f"🔄 Username will auto-refresh every 6 hours."
            f"{kick_line}"
        ),
        parse_mode="HTML",
    )


# ── /setid ────────────────────────────────────────────────────────────────────

@admin_only
async def setid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setid <scammer_db_id> <telegram_id>
    Manually set the Telegram ID for a scammer entry.
    Example: /setid 1 5886335494
    """
    args = context.args
    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        await update.message.reply_text(
            em("Usage: /setid <scammer_#> <telegram_id>\nExample: /setid 1 5886335494"),
            parse_mode="HTML",
        )
        return

    scammer_db_id = int(args[0])
    telegram_id   = int(args[1])

    scammer = await get_scammer_by_id(scammer_db_id)
    if not scammer:
        await update.message.reply_text(em(f"❌ Scammer #{scammer_db_id} not found."), parse_mode="HTML")
        return

    await update_scammer_telegram_id(scammer_db_id, telegram_id, scammer.get("username"))

    uname = f"@{scammer.get('username')}" if scammer.get("username") else "—"
    await update.message.reply_text(
        em(
            f"✅ <b>Updated!</b>\n\n"
            f"Scammer #{scammer_db_id} ({uname})\n"
            f"🔑 Telegram ID set to: <code>{telegram_id}</code>"
        ),
        parse_mode="HTML",
    )


# ── /fixids ───────────────────────────────────────────────────────────────────

@admin_only
async def fixids_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resolve Telegram IDs for all scammers that were added by username only."""
    scammers = await get_scammers_missing_id(batch=200)

    if not scammers:
        await update.message.reply_text(
            em("✅ All scammers already have Telegram IDs!"),
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        em(f"🔄 Found <b>{len(scammers)}</b> scammers without Telegram ID.\nResolving, please wait..."),
        parse_mode="HTML",
    )

    fixed   = 0
    failed  = 0
    details = []

    for s in scammers:
        uname = s.get("username")
        if not uname:
            failed += 1
            continue
        try:
            import asyncio
            await asyncio.sleep(0.5)   # rate-limit safety
            chat = await context.bot.get_chat(f"@{uname}")
            await update_scammer_telegram_id(s["id"], chat.id, chat.username)
            details.append(f"✅ #{s['id']} @{uname} → ID <code>{chat.id}</code>")
            fixed += 1
        except TelegramError as e:
            details.append(f"❌ #{s['id']} @{uname} — {e}")
            failed += 1

    summary = "\n".join(details[:30])  # max 30 lines to avoid message too long
    if len(details) > 30:
        summary += f"\n...and {len(details) - 30} more"

    await update.message.reply_text(
        em(
            f"✅ <b>Fix IDs complete!</b>\n\n"
            f"  Fixed  : <b>{fixed}</b>\n"
            f"  Failed : <b>{failed}</b>\n\n"
            f"{summary}"
        ),
        parse_mode="HTML",
    )
