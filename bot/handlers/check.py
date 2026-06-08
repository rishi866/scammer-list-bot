"""Public /check command — search by @username or Telegram ID.

Auto-update flow
----------------
• Check by ID   → live bot.get_chat(id) before DB lookup; username in DB is
                  always up-to-date after the query.
• Check by @usr → bot.get_chat(@username) gives us the real Telegram ID;
                  we search DB by *both* username AND id, so a scammer who
                  changed their username is still found via their ID.
"""
from __future__ import annotations

import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from bot.db import search_by_telegram_id, search_by_username, update_scammer_username, touch_username_check

logger = logging.getLogger(__name__)


def _format_entry(e: dict) -> str:
    uname   = f"@{e['username']}" if e.get("username") else "—"
    tid     = str(e["telegram_id"]) if e.get("telegram_id") else "—"
    history = e.get("username_history") or []
    hist_str = ", ".join(f"@{u}" for u in history if u) if history else "—"
    return (
        f"🔴 <b>#{e['id']}</b>\n"
        f"  Name: {e.get('name') or '—'}\n"
        f"  Current username: {uname}\n"
        f"  Telegram ID: {tid}\n"
        f"  Past usernames: {hist_str}\n"
        f"  Reason: {e['reason']}\n"
        f"  Proof: {e.get('proof') or '—'}\n"
        f"  Added: {str(e.get('added_at') or '')[:10]}"
    )


async def _live_refresh_by_id(bot, telegram_id: int) -> None:
    """Call getChat and sync username to DB (fire-and-forget, never raises)."""
    try:
        chat = await bot.get_chat(telegram_id)
    except TelegramError as e:
        logger.debug("get_chat(%s) failed during check: %s", telegram_id, e)
        return

    entries = await search_by_telegram_id(telegram_id)
    for entry in entries:
        new_uname = chat.username
        old_uname = entry.get("username")
        if new_uname != old_uname:
            await update_scammer_username(entry["id"], new_uname, old_uname)
            # patch the in-memory entry so the reply shows the fresh username
            entry["username"]         = new_uname
            entry["username_history"] = (entry.get("username_history") or [])
            if old_uname and old_uname not in entry["username_history"]:
                entry["username_history"].append(old_uname)
        else:
            await touch_username_check(entry["id"])


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "  /check @username\n"
            "  /check 123456789  (Telegram ID)",
            parse_mode="HTML",
        )
        return

    query   = args[0].strip()
    is_id   = query.lstrip("@").isdigit()
    results = []
    tg_id   = None

    if is_id:
        tg_id = int(query.lstrip("@"))
        # Live-refresh username before showing results
        await _live_refresh_by_id(context.bot, tg_id)
        results = await search_by_telegram_id(tg_id)
    else:
        # Try to resolve the current @username to a Telegram ID via Telegram API.
        # This catches scammers who changed their username — Telegram still knows
        # the ID behind the handle, so we can find them by ID even if the DB
        # username is stale.
        uname_query = query.lstrip("@")
        resolved_chat = None
        try:
            resolved_chat = await context.bot.get_chat(f"@{uname_query}")
            tg_id = resolved_chat.id
        except TelegramError:
            tg_id = None

        # Search by both stored username AND resolved ID (deduplicated)
        by_username = await search_by_username(uname_query)
        by_id       = await search_by_telegram_id(tg_id) if tg_id else []

        seen = set()
        for e in by_username + by_id:
            if e["id"] not in seen:
                seen.add(e["id"])
                results.append(e)

        # If Telegram gave us a current username, sync any stale DB entries
        if tg_id and resolved_chat:
            for entry in results:
                if entry.get("telegram_id") == tg_id and entry.get("username") != resolved_chat.username:
                    old = entry.get("username")
                    await update_scammer_username(entry["id"], resolved_chat.username, old)
                    entry["username"] = resolved_chat.username
                    hist = entry.get("username_history") or []
                    if old and old not in hist:
                        hist.append(old)
                    entry["username_history"] = hist

    if not results:
        await update.message.reply_text(
            f"✅ <b>Not found</b> — <code>{query}</code> is not in the scammer list.",
            parse_mode="HTML",
        )
        return

    body = "\n\n".join(_format_entry(e) for e in results)
    await update.message.reply_text(
        f"⚠️ <b>Found {len(results)} record(s) for</b> <code>{query}</code>:\n\n{body}",
        parse_mode="HTML",
    )
