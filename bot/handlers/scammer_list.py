"""/scammer_list — paginated list of all confirmed scammers.

Works in both group and private chat.
Navigation is done via inline "Prev / Next" buttons (callback sl_page:<n>).
"""
from __future__ import annotations

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Message
from telegram.ext import ContextTypes

from bot.db import list_scammers, count_scammers

PAGE_SIZE = 8


def _build_page(entries: list[dict], page: int, total: int, total_pages: int) -> tuple[str, InlineKeyboardMarkup | None]:
    lines = []
    for e in entries:
        uname   = f"@{e['username']}" if e.get("username") else "—"
        tid     = f"<code>{e['telegram_id']}</code>" if e.get("telegram_id") else "—"
        history = [u for u in (e.get("username_history") or []) if u]
        old_str = "  |  ".join(f"@{u}" for u in history) if history else "—"
        lines.append(
            f"🔴 <b>#{e['id']}</b>  {uname}  ·  ID: {tid}\n"
            f"   ↪ Old usernames: {old_str}\n"
            f"   ↪ Reason: {(e.get('reason') or '')[:80]}"
        )

    text = (
        f"📋 <b>Confirmed Scammers — {total} total</b>  "
        f"(page {page + 1}/{total_pages})\n\n"
        + "\n\n".join(lines)
    )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"sl_page:{page}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"sl_page:{page + 2}"))

    keyboard = InlineKeyboardMarkup([nav]) if nav else None
    return text, keyboard


async def scammer_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        page = max(1, int((context.args or ["1"])[0])) - 1
    except (ValueError, IndexError):
        page = 0

    total = await count_scammers()
    if total == 0:
        await update.message.reply_text("No confirmed scammers in the list yet.")
        return

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page        = min(page, total_pages - 1)
    entries     = await list_scammers(limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    text, kbd   = _build_page(entries, page, total, total_pages)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kbd)


async def scammer_list_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles sl_page:<n> inline button — edits the existing message in place."""
    query = update.callback_query
    await query.answer()

    page = int(query.data.split(":")[1]) - 1

    total       = await count_scammers()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page        = max(0, min(page, total_pages - 1))
    entries     = await list_scammers(limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    text, kbd   = _build_page(entries, page, total, total_pages)

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kbd)
