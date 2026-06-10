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
