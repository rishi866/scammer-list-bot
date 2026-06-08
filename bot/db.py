"""Database layer — Supabase / PostgreSQL via asyncpg."""
from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")

_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        raise RuntimeError("DB not initialised — call init_db() first")
    return _pool


def _row(record: asyncpg.Record | None) -> Optional[dict]:
    if record is None:
        return None
    d = dict(record)
    if "id" in d and "_id" not in d:
        d["_id"] = d["id"]
    return d


def _rows(records) -> list[dict]:
    return [_row(r) for r in records if r is not None]  # type: ignore[misc]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Init ──────────────────────────────────────────────────────────────────────

async def init_db() -> None:
    global _pool
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set in .env — add your Supabase connection string."
        )
    _pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=2,
        max_size=10,
        statement_cache_size=0,   # required for Supabase/PgBouncer pooler
        command_timeout=10,
    )
    logger.info("Supabase/PostgreSQL pool created")

    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scammers (
                id          BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT,
                username    TEXT,
                name        TEXT,
                reason      TEXT NOT NULL,
                proof       TEXT,
                added_by    BIGINT NOT NULL,
                added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                notes       TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_scammers_telegram_id ON scammers(telegram_id);
            CREATE INDEX IF NOT EXISTS idx_scammers_username    ON scammers(LOWER(username));

            CREATE TABLE IF NOT EXISTS reports (
                id                BIGSERIAL PRIMARY KEY,
                reporter_id       BIGINT NOT NULL,
                reporter_username TEXT,
                target_id         BIGINT,
                target_username   TEXT,
                reason            TEXT NOT NULL,
                proof             TEXT,
                status            TEXT NOT NULL DEFAULT 'pending',
                reported_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
    logger.info("Schema ready")


# ── Scammer CRUD ──────────────────────────────────────────────────────────────

async def add_scammer(
    *,
    telegram_id: Optional[int],
    username: Optional[str],
    name: str,
    reason: str,
    proof: Optional[str],
    added_by: int,
    notes: Optional[str] = None,
) -> int:
    pool = await _get_pool()
    row = await pool.fetchrow(
        """INSERT INTO scammers (telegram_id, username, name, reason, proof, added_by, notes)
           VALUES ($1, $2, $3, $4, $5, $6, $7)
           RETURNING id""",
        telegram_id, username, name, reason, proof, added_by, notes,
    )
    return row["id"]


async def remove_scammer(scammer_id: int) -> bool:
    pool = await _get_pool()
    result = await pool.execute("DELETE FROM scammers WHERE id = $1", scammer_id)
    return result.endswith("1")


async def get_scammer_by_id(scammer_id: int) -> Optional[dict]:
    pool = await _get_pool()
    return _row(await pool.fetchrow("SELECT * FROM scammers WHERE id = $1", scammer_id))


async def search_by_telegram_id(telegram_id: int) -> list[dict]:
    pool = await _get_pool()
    return _rows(await pool.fetch(
        "SELECT * FROM scammers WHERE telegram_id = $1", telegram_id
    ))


async def search_by_username(username: str) -> list[dict]:
    uname = username.lstrip("@").lower()
    pool = await _get_pool()
    return _rows(await pool.fetch(
        "SELECT * FROM scammers WHERE LOWER(username) = $1", uname
    ))


async def list_scammers(limit: int = 50, offset: int = 0) -> list[dict]:
    pool = await _get_pool()
    return _rows(await pool.fetch(
        "SELECT * FROM scammers ORDER BY added_at DESC LIMIT $1 OFFSET $2", limit, offset
    ))


async def count_scammers() -> int:
    pool = await _get_pool()
    return await pool.fetchval("SELECT COUNT(*) FROM scammers")


# ── Reports ───────────────────────────────────────────────────────────────────

async def add_report(
    *,
    reporter_id: int,
    reporter_username: Optional[str],
    target_id: Optional[int],
    target_username: Optional[str],
    reason: str,
    proof: Optional[str],
) -> int:
    pool = await _get_pool()
    row = await pool.fetchrow(
        """INSERT INTO reports
           (reporter_id, reporter_username, target_id, target_username, reason, proof)
           VALUES ($1, $2, $3, $4, $5, $6)
           RETURNING id""",
        reporter_id, reporter_username, target_id, target_username, reason, proof,
    )
    return row["id"]


async def get_report(report_id: int) -> Optional[dict]:
    pool = await _get_pool()
    return _row(await pool.fetchrow("SELECT * FROM reports WHERE id = $1", report_id))


async def list_pending_reports() -> list[dict]:
    pool = await _get_pool()
    return _rows(await pool.fetch(
        "SELECT * FROM reports WHERE status = 'pending' ORDER BY reported_at ASC"
    ))


async def update_report_status(report_id: int, status: str) -> None:
    pool = await _get_pool()
    await pool.execute("UPDATE reports SET status = $1 WHERE id = $2", status, report_id)


async def count_reports(status: Optional[str] = None) -> int:
    pool = await _get_pool()
    if status:
        return await pool.fetchval("SELECT COUNT(*) FROM reports WHERE status = $1", status)
    return await pool.fetchval("SELECT COUNT(*) FROM reports")
