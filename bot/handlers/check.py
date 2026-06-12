"""Public /check command — search by @username, Telegram ID, or real name."""
from __future__ import annotations

import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot.db import (
    search_by_telegram_id, search_by_username,
    search_by_name, search_by_payment_info,
    update_scammer_username, touch_username_check,
)
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)

SEV_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def _format_entry(e: dict) -> str:
    uname    = f"@{e['username']}" if e.get("username") else "—"
    tid      = str(e["telegram_id"]) if e.get("telegram_id") else "—"
    history  = [u for u in (e.get("username_history") or []) if u]
    hist_str = "  |  ".join(f"@{u}" for u in history) if history else "—"
    sev      = (e.get("severity") or "medium").lower()
    sev_icon = SEV_ICON.get(sev, "🟡")
    return em(
        f"🔴 <b>#{e['id']}</b>\n"
        f"  👤 Name: {e.get('name') or '—'}\n"
        f"  📝 Username: {uname}\n"
        f"  🔑 Telegram ID: <code>{tid}</code>\n"
        f"  🔄 Old usernames: {hist_str}\n"
        f"  {sev_icon} Severity: {sev.capitalize()}\n"
        f"  ⚠️ Reason: {e['reason']}\n"
        f"  🔗 Proof: {e.get('proof') or '—'}\n"
        f"  💳 Payment: {e.get('payment_info') or '—'}\n"
        f"  📅 Added: {str(e.get('added_at') or '')[:10]}"
    )


async def _live_refresh_by_id(bot, telegram_id: int, entries: list[dict]) -> None:
    try:
        chat = await bot.get_chat(telegram_id)
    except TelegramError as e:
        logger.debug("get_chat(%s) failed during check: %s", telegram_id, e)
        return

    for entry in entries:
        new_uname = chat.username
        old_uname = entry.get("username")
        if new_uname != old_uname:
            await update_scammer_username(entry["id"], new_uname, old_uname)
            entry["username"]         = new_uname
            hist = list(entry.get("username_history") or [])
            if old_uname and old_uname not in hist:
                hist.append(old_uname)
            entry["username_history"] = hist
        else:
            await touch_username_check(entry["id"])


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            em("🔍 Usage:\n  /check @username\n  /check 123456789  (Telegram ID)\n  /check John Doe  (search by name)\n  /check <binance ID / UPI / wallet address>  (search by payment info)"),
            parse_mode="HTML",
        )
        return

    query   = args[0].strip()
    is_id   = query.lstrip("@").isdigit()
    results = []

    if is_id:
        tg_id   = int(query.lstrip("@"))
        results = await search_by_telegram_id(tg_id)
        if results:
            await _live_refresh_by_id(context.bot, tg_id, results)
    else:
        uname_query   = query.lstrip("@")
        resolved_chat = None
        tg_id         = None
        try:
            resolved_chat = await context.bot.get_chat(f"@{uname_query}")
            tg_id         = resolved_chat.id
        except TelegramError:
            pass

        by_username = await search_by_username(uname_query)
        by_id       = await search_by_telegram_id(tg_id) if tg_id else []

        seen = set()
        for e in by_username + by_id:
            if e["id"] not in seen:
                seen.add(e["id"])
                results.append(e)

        if tg_id and resolved_chat:
            for entry in results:
                if entry.get("telegram_id") == tg_id and entry.get("username") != resolved_chat.username:
                    old = entry.get("username")
                    await update_scammer_username(entry["id"], resolved_chat.username, old)
                    entry["username"] = resolved_chat.username
                    hist = list(entry.get("username_history") or [])
                    if old and old not in hist:
                        hist.append(old)
                    entry["username_history"] = hist

        # Fallback: name search (includes rest of args)
        if not results:
            name_query = " ".join(args).strip()
            results    = await search_by_name(name_query)

    # Final fallback: search by payment info (Binance ID / UPI / wallet address)
    if not results:
        results = await search_by_payment_info(query)

    if not results:
        await update.message.reply_text(
            em(f"✅ <b>Not found</b> — <code>{' '.join(args)}</code> is not in the scammer list."),
            parse_mode="HTML",
        )
        return

    body = "\n\n".join(_format_entry(e) for e in results)
    await update.message.reply_text(
        em(f"⚠️ <b>Found {len(results)} record(s) for</b> <code>{' '.join(args)}</code>:\n\n") + body,
        parse_mode="HTML",
    )
