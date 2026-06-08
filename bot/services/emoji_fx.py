"""Animated / custom emoji helper — adapted from Heaven Store bot.

Usage
-----
Call ``load()`` once at startup (after init_db).  Then in any handler:

    from bot.services.emoji_fx import decorate
    text = decorate("⚠️ Found 1 scammer")
    # → '<tg-emoji emoji-id="12345">⚠️</tg-emoji> Found 1 scammer'

Premium Telegram clients render the animated version; others see the fallback.

Admin adds mappings via /setemoji or /loadpack commands which write to the
``custom_emojis`` table and call reload().
"""
from __future__ import annotations

import re
import logging
import os

logger = logging.getLogger(__name__)

_EMOJI_MAP: dict[str, str] = {}     # fallback_char → custom_emoji_id
_EMOJI_RE: re.Pattern | None = None
_ANDROID_ICONS: list[str] = []      # from TgAndroidIcons sticker set

_VS_RE        = re.compile(r"[︎️]")
_ASCII_WORD_RE= re.compile(r"[A-Za-z0-9_:]")

_SKIP_RE = re.compile(
    r'(<code>.*?</code>|<a\s[^>]*>.*?</a>|<tg-emoji[^>]*>.*?</tg-emoji>)',
    re.DOTALL | re.IGNORECASE,
)

# Sensible keyword → emoji char hints (used when bulk-importing a pack)
KEYWORD_HINTS: dict[str, str] = {
    "✅": "verified",  "❌": "cancel",    "⚠️": "warning",
    "🔴": "scammer",   "📋": "list",      "📨": "report",
    "👤": "user",      "🔍": "search",    "🛡": "shield",
    "🔒": "lock",      "⭐": "star",      "🚀": "fast",
    "🔥": "hot",       "✨": "magic",     "💯": "best",
    "📊": "stats",     "📝": "notes",     "🎉": "approved",
    "📌": "pin",       "📤": "submit",    "🔄": "refresh",
}


def _norm(ch: str) -> str:
    return _VS_RE.sub("", ch or "")


async def load() -> None:
    """Load all custom emoji mappings from the database."""
    from bot.db import list_custom_emojis
    global _EMOJI_MAP, _EMOJI_RE

    items = await list_custom_emojis()
    emoji_map: dict[str, str] = {}

    for item in items:
        fb  = (item.get("fallback") or "").strip()
        cid = (item.get("custom_id") or "").strip()
        if not fb or not cid or _ASCII_WORD_RE.search(fb):
            continue
        for form in {fb, _norm(fb), _norm(fb) + "️"}:
            if form:
                emoji_map.setdefault(form, cid)

    _EMOJI_MAP = emoji_map

    if emoji_map:
        alts = "|".join(re.escape(ch) for ch in sorted(emoji_map, key=len, reverse=True))
        _EMOJI_RE = re.compile(alts)
    else:
        _EMOJI_RE = None

    logger.info("emoji_fx: loaded %d emoji mappings", len(_EMOJI_MAP))


reload = load


async def fetch_pack(pack_name: str) -> list[tuple[str, str]]:
    """Fetch a Telegram sticker/emoji pack and return [(fallback, custom_id), ...]."""
    import httpx
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.telegram.org/bot{token}/getStickerSet",
                params={"name": pack_name},
            )
        data = r.json()
    except Exception as e:
        logger.warning("fetch_pack(%s) HTTP error: %s", pack_name, e)
        return []

    if not data.get("ok"):
        logger.warning("fetch_pack(%s): %s", pack_name, data.get("description"))
        return []

    pairs = []
    for s in data.get("result", {}).get("stickers", []):
        cid = s.get("custom_emoji_id")
        emoji_char = s.get("emoji")
        if cid and emoji_char:
            pairs.append((emoji_char, cid))

    logger.info("fetch_pack(%s): %d stickers fetched", pack_name, len(pairs))
    return pairs


async def bulk_save(pairs: list[tuple[str, str]], *, label: str = "") -> int:
    """Save (fallback_char, custom_id) pairs to DB and reload the cache."""
    from bot.db import upsert_custom_emoji, get_custom_emoji_by_fallback

    seen: set[str] = set()
    for fb, cid in pairs:
        fb  = (fb  or "").strip()
        cid = (cid or "").strip()
        if not fb or not cid or fb in seen or _ASCII_WORD_RE.search(fb):
            continue
        seen.add(fb)
        existing = await get_custom_emoji_by_fallback(fb)
        kw = (existing or {}).get("keyword") or KEYWORD_HINTS.get(fb)
        await upsert_custom_emoji(fallback=fb, custom_id=cid, keyword=kw, label=label or None)

    if seen:
        await load()
    return len(seen)


def decorate(text: str) -> str:
    """Wrap every known emoji char with <tg-emoji emoji-id="..."> tags.

    Content inside <code>, <a>, and existing <tg-emoji> tags is skipped.
    """
    if not text or not _EMOJI_RE:
        return text

    def _sub(m: re.Match) -> str:
        ch  = m.group(0)
        cid = _EMOJI_MAP.get(ch) or _EMOJI_MAP.get(_norm(ch))
        return f'<tg-emoji emoji-id="{cid}">{ch}</tg-emoji>' if cid else ch

    parts = _SKIP_RE.split(text)
    for i, part in enumerate(parts):
        if part and not _SKIP_RE.fullmatch(part):
            parts[i] = _EMOJI_RE.sub(_sub, part)
    return "".join(parts)


# Short alias used in handlers
em = decorate
