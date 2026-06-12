"""Periodic user-directory sync from your OTHER bots' databases.

The scammer bot can resolve a numeric id → username/name only for people it
has "seen". This loop widens that automatically: every few hours it pulls
(telegram_id, username, full_name) from one or more SOURCE databases — e.g.
your shop bot's `users` table — into this bot's `bot_users` cache.

So anyone who interacts with any of your bots (whose DB is listed as a
source) shows up here automatically, with no manual SQL and no userbot.

Configure in .env (all optional — if unset, this loop does nothing):

    SOURCE_DATABASE_URLS=postgres://...A...,postgres://...B...
        Comma-separated Postgres URLs of your other bots' databases. Each
        must have a `users (telegram_id, username, full_name)` table.
        (A single SOURCE_DATABASE_URL also works.)

    USER_SYNC_INTERVAL_HOURS=6
        How often to re-sync (default 6).

The sync runs once immediately on startup, then every interval.
"""
from __future__ import annotations

import asyncio
import logging
import os

import asyncpg

from bot.db import upsert_bot_users_bulk

logger = logging.getLogger(__name__)

SYNC_INTERVAL_HOURS = float(os.getenv("USER_SYNC_INTERVAL_HOURS", "6"))
_DB_CHUNK = 500

# Source table/columns. Matches the shop bot's schema; override the SELECT
# here if a source bot stores users differently.
_SOURCE_QUERY = (
    "SELECT telegram_id, username, full_name "
    "FROM users WHERE telegram_id IS NOT NULL"
)


def _source_urls() -> list[str]:
    raw = os.getenv("SOURCE_DATABASE_URLS", "") or os.getenv("SOURCE_DATABASE_URL", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


async def _sync_one(url: str) -> int:
    """Pull users from one source DB into bot_users. Returns rows synced."""
    conn = await asyncpg.connect(url, statement_cache_size=0, command_timeout=30)
    try:
        rows = await conn.fetch(_SOURCE_QUERY)
    finally:
        await conn.close()

    tuples = [(r["telegram_id"], r["username"], r["full_name"]) for r in rows]
    saved = 0
    for i in range(0, len(tuples), _DB_CHUNK):
        saved += await upsert_bot_users_bulk(tuples[i:i + _DB_CHUNK])
    return saved


async def sync_once() -> int:
    """One sync pass across every configured source. Returns total rows synced."""
    urls = _source_urls()
    if not urls:
        return 0
    total = 0
    for idx, url in enumerate(urls, 1):
        try:
            n = await _sync_one(url)
            total += n
            logger.info("User-sync: pulled %d users from source #%d", n, idx)
        except Exception as e:
            logger.warning("User-sync: source #%d failed: %s", idx, e)
    return total


async def user_sync_loop() -> None:
    """Background loop — syncs source DBs into bot_users every interval."""
    if not _source_urls():
        logger.info("User-sync disabled (no SOURCE_DATABASE_URL[S] in .env)")
        return
    logger.info("User-sync loop started (every %.1fh)", SYNC_INTERVAL_HOURS)
    while True:
        try:
            await sync_once()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("User-sync loop error: %s", e)
        await asyncio.sleep(SYNC_INTERVAL_HOURS * 3600)
