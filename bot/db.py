"""Database layer — SQLite via aiosqlite."""
from __future__ import annotations

import os
import logging
import aiosqlite
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", "scammer_list.db"))

_conn: aiosqlite.Connection | None = None


async def get_conn() -> aiosqlite.Connection:
    global _conn
    if _conn is None:
        _conn = await aiosqlite.connect(DB_PATH)
        _conn.row_factory = aiosqlite.Row
    return _conn


async def init_db() -> None:
    conn = await get_conn()
    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS scammers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            username    TEXT,
            name        TEXT,
            reason      TEXT NOT NULL,
            proof       TEXT,
            added_by    INTEGER NOT NULL,
            added_at    TEXT NOT NULL,
            notes       TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_scammers_telegram_id ON scammers(telegram_id);
        CREATE INDEX IF NOT EXISTS idx_scammers_username ON scammers(username COLLATE NOCASE);

        CREATE TABLE IF NOT EXISTS reports (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_id       INTEGER NOT NULL,
            reporter_username TEXT,
            target_id         INTEGER,
            target_username   TEXT,
            reason            TEXT NOT NULL,
            proof             TEXT,
            status            TEXT NOT NULL DEFAULT 'pending',
            reported_at       TEXT NOT NULL
        );
    """)
    await conn.commit()
    logger.info("DB initialised at %s", DB_PATH)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    conn = await get_conn()
    cur = await conn.execute(
        """INSERT INTO scammers (telegram_id, username, name, reason, proof, added_by, added_at, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (telegram_id, username, name, reason, proof, added_by, _now(), notes),
    )
    await conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def remove_scammer(scammer_id: int) -> bool:
    conn = await get_conn()
    cur = await conn.execute("DELETE FROM scammers WHERE id = ?", (scammer_id,))
    await conn.commit()
    return cur.rowcount > 0


async def get_scammer_by_id(scammer_id: int) -> Optional[dict]:
    conn = await get_conn()
    async with conn.execute("SELECT * FROM scammers WHERE id = ?", (scammer_id,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def search_by_telegram_id(telegram_id: int) -> list[dict]:
    conn = await get_conn()
    async with conn.execute(
        "SELECT * FROM scammers WHERE telegram_id = ?", (telegram_id,)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def search_by_username(username: str) -> list[dict]:
    uname = username.lstrip("@").lower()
    conn = await get_conn()
    async with conn.execute(
        "SELECT * FROM scammers WHERE LOWER(username) = ?", (uname,)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def list_scammers(limit: int = 50, offset: int = 0) -> list[dict]:
    conn = await get_conn()
    async with conn.execute(
        "SELECT * FROM scammers ORDER BY added_at DESC LIMIT ? OFFSET ?", (limit, offset)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def count_scammers() -> int:
    conn = await get_conn()
    async with conn.execute("SELECT COUNT(*) FROM scammers") as cur:
        row = await cur.fetchone()
        return row[0] if row else 0


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
    conn = await get_conn()
    cur = await conn.execute(
        """INSERT INTO reports
           (reporter_id, reporter_username, target_id, target_username, reason, proof, reported_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (reporter_id, reporter_username, target_id, target_username, reason, proof, _now()),
    )
    await conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def get_report(report_id: int) -> Optional[dict]:
    conn = await get_conn()
    async with conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_pending_reports() -> list[dict]:
    conn = await get_conn()
    async with conn.execute(
        "SELECT * FROM reports WHERE status = 'pending' ORDER BY reported_at ASC"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def update_report_status(report_id: int, status: str) -> None:
    conn = await get_conn()
    await conn.execute("UPDATE reports SET status = ? WHERE id = ?", (status, report_id))
    await conn.commit()


async def count_reports(status: Optional[str] = None) -> int:
    conn = await get_conn()
    if status:
        async with conn.execute(
            "SELECT COUNT(*) FROM reports WHERE status = ?", (status,)
        ) as cur:
            row = await cur.fetchone()
    else:
        async with conn.execute("SELECT COUNT(*) FROM reports") as cur:
            row = await cur.fetchone()
    return row[0] if row else 0
