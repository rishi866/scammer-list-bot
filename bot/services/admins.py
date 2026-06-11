"""Centralized admin/owner resolution.

Admin IDs come from THREE sources, merged together:
  1. ADMIN_IDS env var   — static "bootstrap" admins (set in .env, need a
                            restart to change)
  2. OWNER_ID env var    — the bot owner. Falls back to the FIRST entry of
                            ADMIN_IDS if OWNER_ID isn't set, so existing
                            deployments work with zero config changes.
  3. bot_admins DB table — admins added/removed at runtime via /addadmin and
                            /removeadmin (owner only) — take effect instantly,
                            no restart needed.

get_admin_ids() is a SYNC function backed by an in-memory cache so every
existing call site (admin_only decorators, notification loops, etc.) keeps
working unchanged. Call refresh_admin_cache() once at startup (after
init_db()) and it's also called automatically by add_admin()/remove_admin().
"""
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from bot.db import (
    add_admin as _db_add_admin,
    remove_admin as _db_remove_admin,
    list_admins as _db_list_admins,
)
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)

# In-memory cache of DB-added admin IDs, refreshed at startup + on add/remove.
_db_admin_ids: set[int] = set()


def _env_admin_ids() -> set[int]:
    return {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}


def owner_id() -> int:
    """The bot owner's Telegram ID (OWNER_ID env, else first ADMIN_IDS entry)."""
    raw = os.getenv("OWNER_ID", "").strip()
    if raw.isdigit():
        return int(raw)
    for x in os.getenv("ADMIN_IDS", "").split(","):
        x = x.strip()
        if x.isdigit():
            return int(x)
    return 0


def is_owner(user_id: int) -> bool:
    oid = owner_id()
    return oid != 0 and user_id == oid


def get_admin_ids() -> set[int]:
    """All current admin Telegram IDs — env + owner + DB-added admins."""
    ids = _env_admin_ids() | _db_admin_ids
    oid = owner_id()
    if oid:
        ids.add(oid)
    return ids


async def refresh_admin_cache() -> None:
    """Reload the DB-added-admins cache. Call at startup and after add/remove."""
    global _db_admin_ids
    rows = await _db_list_admins()
    _db_admin_ids = {r["telegram_id"] for r in rows}
    logger.info("Admin cache: owner=%s, env=%d, db=%d",
                 owner_id() or "—", len(_env_admin_ids()), len(_db_admin_ids))


async def add_admin(telegram_id: int, added_by: int) -> bool:
    """Grant admin rights to telegram_id. Returns False if already an admin."""
    if telegram_id in get_admin_ids():
        return False
    await _db_add_admin(telegram_id, added_by)
    await refresh_admin_cache()
    return True


async def remove_admin(telegram_id: int) -> str:
    """Revoke admin rights from telegram_id.

    Returns "removed", "owner" (can't remove the owner), "env" (set via
    ADMIN_IDS, needs a .env edit + restart), or "not_found".
    """
    if is_owner(telegram_id):
        return "owner"
    if telegram_id not in _db_admin_ids:
        return "env" if telegram_id in _env_admin_ids() else "not_found"
    await _db_remove_admin(telegram_id)
    await refresh_admin_cache()
    return "removed"


async def list_all_admins() -> list[dict]:
    """Combined, deduplicated view: owner first, then env admins, then DB admins."""
    rows   = await _db_list_admins()
    db_map = {r["telegram_id"]: r for r in rows}

    out: list[dict] = []
    seen: set[int] = set()

    oid = owner_id()
    if oid:
        out.append({"telegram_id": oid, "source": "owner"})
        seen.add(oid)

    for aid in sorted(_env_admin_ids()):
        if aid in seen:
            continue
        out.append({"telegram_id": aid, "source": "env"})
        seen.add(aid)

    for aid, row in db_map.items():
        if aid in seen:
            continue
        out.append({"telegram_id": aid, "source": "db", **row})
        seen.add(aid)

    return out


# ── Protect owner/admins from being reported as scammers ──────────────────────

async def resolve_protected_role(
    telegram_id: Optional[int] = None,
    username: Optional[str] = None,
    bot=None,
) -> Optional[str]:
    """Return "owner" / "admin" if telegram_id or @username belongs to the
    bot's owner or one of its admins, else None.

    - telegram_id is checked directly when known.
    - For @username-only targets (no resolved ID yet), first tries the
      passive bot_users cache (built from group activity), then — if a
      `bot` instance is given — falls back to a live get_chat("@username")
      lookup. Either way, the result is checked against the same owner/admin
      ID sets.
    """
    def _role(uid: Optional[int]) -> Optional[str]:
        if not uid:
            return None
        if is_owner(uid):
            return "owner"
        if uid in get_admin_ids():
            return "admin"
        return None

    role = _role(telegram_id)
    if role:
        return role

    if username:
        uname = username.lstrip("@")
        from bot.db import get_bot_user_by_username
        cached = await get_bot_user_by_username(uname)
        role = _role(cached["telegram_id"]) if cached else None
        if role:
            return role

        if bot is not None:
            try:
                chat = await bot.get_chat(f"@{uname}")
                role = _role(chat.id)
                if role:
                    return role
            except Exception:
                pass

    return None


def protected_block_message(role: str) -> str:
    """User-facing message shown when a report/add target is the owner/admin."""
    who = "👑 the <b>owner</b>" if role == "owner" else "🛠 an <b>admin</b>"
    return (
        f"🚫 <b>Action blocked</b>\n\n"
        f"This account belongs to {who} of this bot — it can't be reported, "
        f"added, or listed as a scammer."
    )


# ── Decorator: owner-only commands (admin management) ─────────────────────────

def owner_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            await update.message.reply_text(
                em("⛔ Owner only — only the bot owner can manage admins."),
                parse_mode="HTML",
            )
            return
        return await func(update, context)
    return wrapper
