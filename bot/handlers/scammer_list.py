"""/scammer_list — paginated list of all confirmed scammers."""
from __future__ import annotations

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from bot.db import list_scammers, count_scammers
from bot.services.emoji_fx import em

PAGE_SIZE = 8


def _build_page(entries: list[dict], page: int, total: int, total_pages: int) -> tuple[str, InlineKeyboardMarkup | None]:
    SEV_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    lines = []
    for idx, e in enumerate(entries):
        seq_num  = page * PAGE_SIZE + idx + 1   # sequential: 1,2,3,4…
        uname    = f"@{e['username']}" if e.get("username") else "—"
        tid      = f"<code>{e['telegram_id']}</code>" if e.get("telegram_id") else "—"
        history  = [u for u in (e.get("username_history") or []) if u]
        old_str  = "  |  ".join(f"@{u}" for u in history) if history else "—"
        sev      = (e.get("severity") or "medium").lower()
        sev_icon = SEV_ICON.get(sev, "🟡")
        lines.append(em(
            f"{sev_icon} <b>#{seq_num}</b>  {uname}  ·  🔑 ID: {tid}\n"
            f"   🔄 Old usernames: {old_str}\n"
            f"   ⚠️ Reason: {(e.get('reason') or '')[:80]}\n"
            f"   🗑 <code>/remove {e['id']}</code>"
        ))

    header = em(
        f"📋 <b>Confirmed Scammers — {total} total</b>  "
        f"(page {page + 1}/{total_pages})\n\n"
    )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"sl_page:{page}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"sl_page:{page + 2}"))

    keyboard = InlineKeyboardMarkup([nav]) if nav else None
    return header + "\n\n".join(lines), keyboard


async def scammer_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        page = max(1, int((context.args or ["1"])[0])) - 1
    except (ValueError, IndexError):
        page = 0

    total = await count_scammers()
    if total == 0:
        await update.message.reply_text(em("📋 No confirmed scammers in the list yet."), parse_mode="HTML")
        return

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page        = min(page, total_pages - 1)
    entries     = await list_scammers(limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    text, kbd   = _build_page(entries, page, total, total_pages)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kbd)


async def scammer_list_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    page = int(query.data.split(":")[1]) - 1

    total       = await count_scammers()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page        = max(0, min(page, total_pages - 1))
    entries     = await list_scammers(limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    text, kbd   = _build_page(entries, page, total, total_pages)

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kbd)
