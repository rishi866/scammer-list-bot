"""Public /check command — search by @username or Telegram ID."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from bot.db import search_by_telegram_id, search_by_username


def _format_entries(entries: list[dict]) -> str:
    parts = []
    for e in entries:
        uname = f"@{e['username']}" if e.get("username") else "—"
        tid = str(e["telegram_id"]) if e.get("telegram_id") else "—"
        parts.append(
            f"🔴 <b>#{e['id']}</b>\n"
            f"  Name: {e.get('name') or '—'}\n"
            f"  Username: {uname}\n"
            f"  Telegram ID: {tid}\n"
            f"  Reason: {e['reason']}\n"
            f"  Proof: {e.get('proof') or '—'}\n"
            f"  Added: {(e.get('added_at') or '')[:10]}"
        )
    return "\n\n".join(parts)


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /check @username  or  /check 123456789",
            parse_mode="HTML",
        )
        return

    query = args[0].strip()

    if query.lstrip("@").isdigit():
        results = await search_by_telegram_id(int(query.lstrip("@")))
    else:
        results = await search_by_username(query)

    if not results:
        await update.message.reply_text(
            f"✅ <b>Not found</b> — <code>{query}</code> is not in the scammer list.",
            parse_mode="HTML",
        )
        return

    text = (
        f"⚠️ <b>Found {len(results)} record(s) for</b> <code>{query}</code>:\n\n"
        + _format_entries(results)
    )
    await update.message.reply_text(text, parse_mode="HTML")
