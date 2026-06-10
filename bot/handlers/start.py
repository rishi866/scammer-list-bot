import os
import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.error import TelegramError
from bot.db import search_by_telegram_id, count_scammers, upsert_bot_user
from bot.services.admins import get_admin_ids as _admin_ids, is_owner
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)

SEV_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def _welcome_text(count: int, is_admin: bool = False, is_owner_user: bool = False) -> str:
    text = (
        "✨ <b>Scammer List Bot</b>\n\n"
        "Report and verify Telegram scammers to keep the community safe.\n"
        f"📊 Currently tracking <b>{count}</b> scammer(s).\n\n"

        "<b>🔍 Check Someone</b>\n"
        "• /check @username — search by username\n"
        "• /check 123456789 — search by Telegram ID\n"
        "• /check John Doe — search by name\n\n"

        "<b>🚨 Report a Scammer</b> (open to everyone — admin approves before listing)\n"
        "• 📨 /report — guided report (PM: full form + proof · group: quick "
        "<code>/report @user reason</code>)\n"
        "• 🔑 /addid &lt;id&gt; [reason] — report by Telegram ID, I auto-fetch "
        "their name &amp; username (works in PM &amp; groups)\n"
        "• 📝 /add @username [reason] — report inside a group "
        "(reply to a photo to attach proof)\n"
        "• ↪️ <b>Forward</b> a scammer's message to me — in PM I'll start a "
        "report; in groups I'll check if they're already listed\n\n"

        "<b>⚖️ Wrongly Listed?</b>\n"
        "• /appeal &lt;reason&gt; — dispute your listing in PM, admin will review\n\n"

        "<b>📋 Other</b>\n"
        "• /scammer_list — view all confirmed scammers\n"
        "• /help — show this message\n\n"

        "✅ Approved reports are added to the list, broadcast to every group, "
        "and the scammer is auto-kicked/banned wherever I'm admin."
    )

    if is_admin:
        text += (
            "\n\n<b>🛠 Admin Tools</b>\n"
            "• /pending · /approve &lt;#&gt; · /reject &lt;#&gt; — review submissions\n"
            "• /edit &lt;#&gt; &lt;field&gt; &lt;value&gt; — fix reason/severity/username/name/id\n"
            "• /remove &lt;#&gt; · /list · /stats · /fixids · /setid\n"
            "• /addchannel · /listchannels · /removechannel &lt;#&gt; — require "
            "users to join channel(s)/group(s) before using the bot in PM\n"
            "• /listadmins — view all bot admins"
        )

    if is_owner_user:
        text += (
            "\n\n<b>👑 Owner Tools</b>\n"
            "• /addadmin &lt;telegram_id&gt; — grant admin access\n"
            "• /removeadmin &lt;telegram_id&gt; — revoke admin access"
        )

    return text


def _quick_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔍 Check",  callback_data="qa_check"),
        InlineKeyboardButton("📨 Report", callback_data="qa_report"),
        InlineKeyboardButton("📋 List",   callback_data="qa_list"),
    ]])


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user      = update.effective_user
    full_name = " ".join(filter(None, [user.first_name, user.last_name])) or "—"
    username  = f"@{user.username}" if user.username else "—"

    # Send welcome to user
    count    = await count_scammers()
    is_admin = user.id in _admin_ids()
    await update.message.reply_text(
        em(_welcome_text(count, is_admin, is_owner(user.id))),
        parse_mode="HTML",
        reply_markup=_quick_keyboard(),
    )

    # Track first-time vs returning users (avoid repeat "New User Joined" spam)
    is_new_user = await upsert_bot_user(user.id, user.username, full_name)

    # Check if this user is a known scammer
    scammer_records = await search_by_telegram_id(user.id)

    notif = None
    if scammer_records:
        # 🚨 SCAMMER ALERT to admins (every time — worth re-flagging)
        e        = scammer_records[0]
        sev      = (e.get("severity") or "medium").lower()
        sev_icon = SEV_ICON.get(sev, "🟡")
        history  = [u for u in (e.get("username_history") or []) if u]
        hist_str = ", ".join(f"@{u}" for u in history) if history else "—"

        notif = (
            f"🚨 <b>ALERT: Known Scammer Started the Bot!</b>\n\n"
            f"👤 Name     : <b>{full_name}</b>\n"
            f"📝 Username : {username}\n"
            f"🔑 User ID  : <code>{user.id}</code>\n\n"
            f"📋 <b>Listed as Scammer #{e['id']}</b>\n"
            f"{sev_icon} Severity  : {sev.capitalize()}\n"
            f"⚠️ Reason   : {e['reason']}\n"
            f"🔄 Past usernames: {hist_str}"
        )
    elif is_new_user:
        # Normal new user notification — only the FIRST time they /start
        notif = (
            f"🆕 <b>New User Joined</b>\n\n"
            f"👤 Name    : <b>{full_name}</b>\n"
            f"📝 Username: {username}\n"
            f"🔑 User ID : <code>{user.id}</code>"
        )

    if notif:
        for aid in _admin_ids():
            if aid == user.id:
                continue
            try:
                await context.bot.send_message(aid, notif, parse_mode="HTML")
            except TelegramError as e:
                logger.warning("Could not notify admin %s: %s", aid, e)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user     = update.effective_user
    count    = await count_scammers()
    is_admin = user.id in _admin_ids()
    await update.message.reply_text(
        em(_welcome_text(count, is_admin, is_owner(user.id))),
        parse_mode="HTML",
        reply_markup=_quick_keyboard(),
    )


# ── Quick-action buttons (callback_data: qa_check / qa_report / qa_list) ──────

async def qa_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer(
        "Send:\n/check @username\n/check <Telegram ID>\n/check Full Name",
        show_alert=True,
    )


async def qa_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query     = update.callback_query
    chat_type = update.effective_chat.type
    if chat_type == "private":
        msg = ("Send /report to start a guided report (with proof), "
               "or /addid <id> reason for a quick report.")
    else:
        msg = "Use: /report @username reason\nor: /report <Telegram ID> reason"
    await query.answer(msg, show_alert=True)


async def qa_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from bot.handlers.scammer_list import _build_page, PAGE_SIZE
    from bot.db import list_scammers

    query = update.callback_query
    total = await count_scammers()
    if total == 0:
        await query.answer("No confirmed scammers yet.", show_alert=True)
        return

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    entries     = await list_scammers(limit=PAGE_SIZE, offset=0)
    text, kbd   = _build_page(entries, 0, total, total_pages)
    await query.message.reply_text(text, parse_mode="HTML", reply_markup=kbd)
