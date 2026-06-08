"""Admin commands to manage animated emoji mappings.

/setemoji <emoji> <custom_id>   — map one emoji char to an animated ID
/delemoji <emoji>               — remove a mapping
/listemoji                      — show all current mappings
/loadpack <pack_name>           — import an entire Telegram sticker pack
                                  (e.g. /loadpack TgAndroidIcons)
"""
from __future__ import annotations

import os
import logging
from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes

from bot.db import list_custom_emojis, upsert_custom_emoji, delete_custom_emoji
from bot.services import emoji_fx

logger = logging.getLogger(__name__)


def _admin_ids() -> set[int]:
    return {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in _admin_ids():
            await update.message.reply_text("⛔ Admins only.")
            return
        return await func(update, context)
    return wrapper


@admin_only
async def setemoji_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /setemoji <emoji_char> <custom_emoji_id>"""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /setemoji &lt;emoji&gt; &lt;custom_emoji_id&gt;\n"
            "Example: /setemoji 🔴 5260932198754816254",
            parse_mode="HTML",
        )
        return

    fb  = args[0].strip()
    cid = args[1].strip()

    if not cid.isdigit():
        await update.message.reply_text("❌ custom_emoji_id must be a numeric string.")
        return

    await upsert_custom_emoji(fallback=fb, custom_id=cid)
    await emoji_fx.reload()
    await update.message.reply_text(
        f"✅ Mapped {fb} → <code>{cid}</code>\n"
        f"Preview: <tg-emoji emoji-id=\"{cid}\">{fb}</tg-emoji>",
        parse_mode="HTML",
    )


@admin_only
async def delemoji_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /delemoji <emoji_char>"""
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /delemoji &lt;emoji&gt;", parse_mode="HTML")
        return

    fb = args[0].strip()
    ok = await delete_custom_emoji(fb)
    await emoji_fx.reload()
    if ok:
        await update.message.reply_text(f"🗑 Removed mapping for {fb}")
    else:
        await update.message.reply_text(f"❌ No mapping found for {fb}")


@admin_only
async def listemoji_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    items = await list_custom_emojis()
    if not items:
        await update.message.reply_text("No emoji mappings configured yet.\nUse /setemoji or /loadpack.")
        return

    # Bot ke specific emojis check karo
    BOT_EMOJIS = ["✨","🔍","📝","📋","📨","ℹ️","✅","❌","⚠️","🔴","🟡","🟢",
                  "👤","🔑","🔄","📅","🔗","📌","📤","📊","🚨","🛡","🏆","🎉"]
    mapped_set = {it.get("fallback","") for it in items}

    found   = [e for e in BOT_EMOJIS if e in mapped_set]
    missing = [e for e in BOT_EMOJIS if e not in mapped_set]

    status = (
        f"<b>📊 Emoji Mappings: {len(items)} total</b>\n\n"
        f"✅ Bot emojis mapped ({len(found)}): {' '.join(found) or '—'}\n"
        f"❌ Bot emojis missing ({len(missing)}): {' '.join(missing) or '—'}\n\n"
    )

    # Show first 30 only to avoid message length limit
    preview_lines = []
    for it in items[:30]:
        fb  = it.get("fallback", "?")
        cid = it.get("custom_id", "?")
        preview_lines.append(f'<tg-emoji emoji-id="{cid}">{fb}</tg-emoji> {fb}')

    status += "<b>Preview (first 30):</b>\n" + "  ".join(preview_lines)
    if len(items) > 30:
        status += f"\n\n…and {len(items) - 30} more"

    await update.message.reply_text(status, parse_mode="HTML")


@admin_only
async def loadpack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /loadpack <sticker_pack_name>
    Fetches the pack from Telegram and imports all custom emoji IDs.
    """
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /loadpack &lt;pack_name&gt;\n"
            "Example: /loadpack TgAndroidIcons",
            parse_mode="HTML",
        )
        return

    pack_name = args[0].strip()
    msg = await update.message.reply_text(f"⏳ Fetching pack <code>{pack_name}</code>…", parse_mode="HTML")

    pairs = await emoji_fx.fetch_pack(pack_name)
    if not pairs:
        await msg.edit_text(f"❌ Could not fetch pack <code>{pack_name}</code>. Check the pack name.", parse_mode="HTML")
        return

    saved = await emoji_fx.bulk_save(pairs, label=pack_name)
    await msg.edit_text(
        f"✅ Pack <b>{pack_name}</b> imported!\n"
        f"  Stickers fetched: {len(pairs)}\n"
        f"  Unique emoji saved: {saved}\n\n"
        f"Use /listemoji to see all mappings.",
        parse_mode="HTML",
    )
