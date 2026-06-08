"""Admin-only commands: /add /remove /list /pending /approve /reject /stats."""
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Callable

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters

from bot.db import (
    add_scammer,
    remove_scammer,
    get_scammer_by_id,
    list_scammers,
    count_scammers,
    list_pending_reports,
    get_report,
    update_report_status,
    count_reports,
)
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)

ADD_TARGET, ADD_NAME, ADD_REASON, ADD_PROOF, ADD_NOTES = range(5)


def _get_admin_ids() -> set[int]:
    return {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}


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
            "➕ <b>Add Scammer</b> — Step 1/5\n\n"
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
                    f"👤 Step 2/5 — Confirm or change their full name\n"
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
                   f"👤 Step 2/5 — Enter their full name manually:"),
                parse_mode="HTML",
            )
    return ADD_NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    # "auto" = keep the Telegram-fetched name
    if raw.lower() == "auto" and context.user_data.get("add_target_name"):
        context.user_data["add_name"] = context.user_data["add_target_name"]
    else:
        context.user_data["add_name"] = raw
    await update.message.reply_text(em("⚠️ Step 3/5 — Reason for listing:"), parse_mode="HTML")
    return ADD_REASON


async def add_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["add_reason"] = update.message.text.strip()
    await update.message.reply_text(
        em("🔗 Step 4/5 — Proof link or description (or <b>none</b>):"),
        parse_mode="HTML",
    )
    return ADD_PROOF


async def add_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    context.user_data["add_proof"] = None if raw.lower() == "none" else raw
    await update.message.reply_text(
        em("📝 Step 5/5 — Additional notes (or <b>none</b>):"),
        parse_mode="HTML",
    )
    return ADD_NOTES


async def add_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw   = update.message.text.strip()
    notes = None if raw.lower() == "none" else raw

    scammer_id = await add_scammer(
        telegram_id = context.user_data.get("add_target_id"),
        username    = context.user_data.get("add_target_uname"),
        name        = context.user_data["add_name"],
        reason      = context.user_data["add_reason"],
        proof       = context.user_data.get("add_proof"),
        added_by    = update.effective_user.id,
        notes       = notes,
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
            ADD_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_target)],
            ADD_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            ADD_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_reason)],
            ADD_PROOF:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_proof)],
            ADD_NOTES:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_notes)],
        },
        fallbacks=[CommandHandler("cancel", add_cancel)],
    )


# ── /remove ───────────────────────────────────────────────────────────────────

@admin_only
async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /remove <id>")
        return
    sid = int(args[0])
    ok  = await remove_scammer(sid)
    if ok:
        await update.message.reply_text(em(f"✅ Scammer #{sid} removed."), parse_mode="HTML")
    else:
        await update.message.reply_text(em(f"❌ No entry with ID #{sid}."), parse_mode="HTML")


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
        telegram_id = report.get("target_id"),
        username    = report.get("target_username"),
        name        = report.get("target_full_name") or report.get("target_username") or str(report.get("target_id") or "Unknown"),
        reason      = report["reason"],
        proof       = report.get("proof"),
        added_by    = update.effective_user.id,
    )
    await update_report_status(rid, "approved")
    await update.message.reply_text(
        em(f"✅ Report #{rid} approved — added as <b>#{scammer_id}</b>."),
        parse_mode="HTML",
    )


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
