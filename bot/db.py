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
                id                  BIGSERIAL PRIMARY KEY,
                telegram_id         BIGINT,
                username            TEXT,
                username_history    TEXT[]       DEFAULT '{}',
                last_username_check TIMESTAMPTZ,
                name                TEXT,
                reason              TEXT NOT NULL,
                proof               TEXT,
                added_by            BIGINT NOT NULL,
                added_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                notes               TEXT,
                payment_info        TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_scammers_telegram_id ON scammers(telegram_id);
            CREATE INDEX IF NOT EXISTS idx_scammers_username    ON scammers(LOWER(username));

            CREATE TABLE IF NOT EXISTS reports (
                id                BIGSERIAL PRIMARY KEY,
                reporter_id       BIGINT NOT NULL,
                reporter_username TEXT,
                target_id         BIGINT,
                target_username   TEXT,
                target_full_name  TEXT,
                reason            TEXT NOT NULL,
                proof             TEXT,
                payment_info      TEXT,
                group_chat_id     BIGINT,
                status            TEXT NOT NULL DEFAULT 'pending',
                reported_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS custom_emojis (
                id         BIGSERIAL PRIMARY KEY,
                fallback   TEXT NOT NULL UNIQUE,
                custom_id  TEXT NOT NULL,
                keyword    TEXT,
                label      TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS bot_groups (
                group_id  BIGINT PRIMARY KEY,
                title     TEXT,
                added_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                active    BOOLEAN NOT NULL DEFAULT TRUE
            );

            CREATE TABLE IF NOT EXISTS trusted_reporters (
                user_id   BIGINT PRIMARY KEY,
                username  TEXT,
                added_by  BIGINT NOT NULL,
                added_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS appeals (
                id          BIGSERIAL PRIMARY KEY,
                scammer_id  BIGINT NOT NULL,
                telegram_id BIGINT,
                username    TEXT,
                message     TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_appeals_scammer_id ON appeals(scammer_id);

            CREATE TABLE IF NOT EXISTS required_channels (
                id          BIGSERIAL PRIMARY KEY,
                chat_id     BIGINT,
                username    TEXT,
                title       TEXT,
                invite_link TEXT NOT NULL,
                added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS bot_admins (
                telegram_id BIGINT PRIMARY KEY,
                added_by    BIGINT,
                added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS bot_users (
                telegram_id BIGINT PRIMARY KEY,
                username    TEXT,
                full_name   TEXT,
                first_seen  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS admin_actions (
                id             BIGSERIAL PRIMARY KEY,
                actor_id       BIGINT,
                actor_username TEXT,
                action         TEXT NOT NULL,
                target_type    TEXT,
                target_id      TEXT,
                detail         TEXT,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_admin_actions_time ON admin_actions (created_at DESC);
        """)
        # Idempotent migrations for columns added after initial deploy
        for tbl, col, definition in [
            ("scammers", "username_history",    "TEXT[] DEFAULT '{}'"),
            ("scammers", "last_username_check", "TIMESTAMPTZ"),
            ("scammers", "severity",            "TEXT NOT NULL DEFAULT 'medium'"),
            ("scammers", "proof_file_id",       "TEXT"),
            ("reports",  "target_full_name",    "TEXT"),
            ("reports",  "group_chat_id",       "BIGINT"),
            ("reports",  "proof_file_id",       "TEXT"),
            ("bot_users", "username_history",   "TEXT[] DEFAULT '{}'"),
            ("scammers", "payment_info",        "TEXT"),
            ("reports",  "payment_info",        "TEXT"),
        ]:
            await conn.execute(
                f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} {definition};"
            )
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
    severity: str = "medium",
    proof_file_id: Optional[str] = None,
    payment_info: Optional[str] = None,
) -> int:
    pool = await _get_pool()
    row = await pool.fetchrow(
        """INSERT INTO scammers
           (telegram_id, username, name, reason, proof, added_by, notes, severity, proof_file_id, payment_info)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
           RETURNING id""",
        telegram_id, username, name, reason, proof, added_by, notes, severity, proof_file_id, payment_info,
    )
    return row["id"]


async def remove_scammer(scammer_id: int) -> bool:
    pool = await _get_pool()
    result = await pool.execute("DELETE FROM scammers WHERE id = $1", scammer_id)
    return result.endswith("1")


async def get_scammer_by_id(scammer_id: int) -> Optional[dict]:
    pool = await _get_pool()
    return _row(await pool.fetchrow("SELECT * FROM scammers WHERE id = $1", scammer_id))


async def get_scammer_by_seq(seq: int) -> Optional[dict]:
    """Get scammer by 1-based sequential position as shown in /scammer_list."""
    pool = await _get_pool()
    return _row(await pool.fetchrow(
        "SELECT * FROM scammers ORDER BY added_at DESC LIMIT 1 OFFSET $1",
        seq - 1,
    ))


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


# ── Edit scammer fields ───────────────────────────────────────────────────────

# Maps the user-facing field name (used in /edit) → actual DB column.
EDITABLE_FIELDS = {
    "reason":       "reason",
    "severity":     "severity",
    "username":     "username",
    "name":         "name",
    "id":           "telegram_id",
    "telegram_id":  "telegram_id",
    "notes":        "notes",
    "proof":        "proof",
    "payment":      "payment_info",
    "payment_info": "payment_info",
}


async def update_scammer_field(scammer_id: int, field: str, value) -> bool:
    """Update a single column of a scammer entry.

    `field` must be a key in EDITABLE_FIELDS (validated by caller) — this
    keeps the interpolated column name restricted to a fixed whitelist.
    """
    column = EDITABLE_FIELDS.get(field)
    if not column:
        return False
    pool = await _get_pool()
    result = await pool.execute(f"UPDATE scammers SET {column} = $1 WHERE id = $2", value, scammer_id)
    return result.endswith("1")


async def update_scammer_fields(scammer_id: int, fields: dict) -> bool:
    """Update multiple columns of a scammer entry at once.

    `fields` keys must be in EDITABLE_FIELDS (validated by caller) — this
    keeps the interpolated column names restricted to a fixed whitelist.
    """
    columns = {EDITABLE_FIELDS[f]: v for f, v in fields.items() if f in EDITABLE_FIELDS}
    if not columns:
        return False
    pool = await _get_pool()
    set_clause = ", ".join(f"{col} = ${i + 1}" for i, col in enumerate(columns))
    values = list(columns.values())
    result = await pool.execute(
        f"UPDATE scammers SET {set_clause} WHERE id = ${len(values) + 1}",
        *values, scammer_id,
    )
    return result.endswith("1")


# ── Reports ───────────────────────────────────────────────────────────────────

async def add_report(
    *,
    reporter_id: int,
    reporter_username: Optional[str],
    target_id: Optional[int],
    target_username: Optional[str],
    target_full_name: Optional[str] = None,
    reason: str,
    proof: Optional[str],
    group_chat_id: Optional[int] = None,
    proof_file_id: Optional[str] = None,
    payment_info: Optional[str] = None,
) -> int:
    pool = await _get_pool()
    row = await pool.fetchrow(
        """INSERT INTO reports
           (reporter_id, reporter_username, target_id, target_username,
            target_full_name, reason, proof, group_chat_id, proof_file_id, payment_info)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
           RETURNING id""",
        reporter_id, reporter_username, target_id, target_username,
        target_full_name, reason, proof, group_chat_id, proof_file_id, payment_info,
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


# ── Username auto-update ───────────────────────────────────────────────────────

async def update_scammer_username(scammer_id: int, new_username: Optional[str], old_username: Optional[str]) -> None:
    """Set current username; push old one into history if not already there."""
    pool = await _get_pool()
    if old_username:
        await pool.execute(
            """
            UPDATE scammers
            SET
                username            = $1,
                username_history    = CASE
                    WHEN username_history IS NULL           THEN ARRAY[$2]::TEXT[]
                    WHEN $2 = ANY(username_history)         THEN username_history
                    ELSE array_append(username_history, $2)
                END,
                last_username_check = NOW()
            WHERE id = $3
            """,
            new_username, old_username, scammer_id,
        )
    else:
        await pool.execute(
            "UPDATE scammers SET username = $1, last_username_check = NOW() WHERE id = $2",
            new_username, scammer_id,
        )


async def touch_username_check(scammer_id: int) -> None:
    """Record that we checked this scammer's username right now (no change found)."""
    pool = await _get_pool()
    await pool.execute(
        "UPDATE scammers SET last_username_check = NOW() WHERE id = $1", scammer_id
    )


# ── Custom Emojis ─────────────────────────────────────────────────────────────

async def list_custom_emojis() -> list[dict]:
    pool = await _get_pool()
    return _rows(await pool.fetch("SELECT * FROM custom_emojis ORDER BY created_at ASC"))


async def get_custom_emoji_by_fallback(fallback: str) -> Optional[dict]:
    pool = await _get_pool()
    return _row(await pool.fetchrow("SELECT * FROM custom_emojis WHERE fallback = $1", fallback))


async def upsert_custom_emoji(
    *, fallback: str, custom_id: str, keyword: Optional[str] = None, label: Optional[str] = None
) -> None:
    pool = await _get_pool()
    await pool.execute(
        """INSERT INTO custom_emojis (fallback, custom_id, keyword, label)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (fallback) DO UPDATE
             SET custom_id = EXCLUDED.custom_id,
                 keyword   = COALESCE(EXCLUDED.keyword, custom_emojis.keyword),
                 label     = COALESCE(EXCLUDED.label,   custom_emojis.label)""",
        fallback, custom_id, keyword, label,
    )


async def delete_custom_emoji(fallback: str) -> bool:
    pool = await _get_pool()
    result = await pool.execute("DELETE FROM custom_emojis WHERE fallback = $1", fallback)
    return result.endswith("1")


# ── Username refresh ───────────────────────────────────────────────────────────

async def get_scammers_missing_id(batch: int = 200) -> list[dict]:
    """Return scammers that have a username but no telegram_id yet."""
    pool = await _get_pool()
    return _rows(await pool.fetch(
        """SELECT * FROM scammers
           WHERE telegram_id IS NULL
             AND username IS NOT NULL
           ORDER BY id ASC
           LIMIT $1""",
        batch,
    ))


async def update_scammer_telegram_id(scammer_id: int, telegram_id: int, username: Optional[str]) -> None:
    """Set telegram_id for a scammer that was added without one."""
    pool = await _get_pool()
    await pool.execute(
        """UPDATE scammers
           SET telegram_id = $1,
               username    = COALESCE($2, username),
               last_username_check = NOW()
           WHERE id = $3""",
        telegram_id, username, scammer_id,
    )


async def get_scammers_needing_refresh(stale_hours: int = 6, batch: int = 100) -> list[dict]:
    pool = await _get_pool()
    return _rows(await pool.fetch(
        """SELECT * FROM scammers
           WHERE telegram_id IS NOT NULL
             AND (last_username_check IS NULL
                  OR last_username_check < NOW() - ($1 * INTERVAL '1 hour'))
           ORDER BY last_username_check ASC NULLS FIRST
           LIMIT $2""",
        stale_hours, batch,
    ))


# ── Name search ───────────────────────────────────────────────────────────────

async def search_by_name(name: str) -> list[dict]:
    pool = await _get_pool()
    return _rows(await pool.fetch(
        "SELECT * FROM scammers WHERE LOWER(name) LIKE $1", f"%{name.lower()}%"
    ))


async def search_by_payment_info(text: str) -> list[dict]:
    """Find scammers whose payment_info (Binance ID, UPI, wallet address, etc.) contains text."""
    pool = await _get_pool()
    return _rows(await pool.fetch(
        "SELECT * FROM scammers WHERE payment_info IS NOT NULL AND LOWER(payment_info) LIKE $1",
        f"%{text.lower()}%",
    ))


# ── Duplicate check ───────────────────────────────────────────────────────────

async def scammer_exists(telegram_id: Optional[int], username: Optional[str]) -> Optional[dict]:
    """Return first matching scammer by ID or username, or None."""
    pool = await _get_pool()
    if telegram_id:
        row = await pool.fetchrow("SELECT * FROM scammers WHERE telegram_id = $1 LIMIT 1", telegram_id)
        if row:
            return _row(row)
    if username:
        row = await pool.fetchrow(
            "SELECT * FROM scammers WHERE LOWER(username) = $1 LIMIT 1", username.lower()
        )
        if row:
            return _row(row)
    return None


async def cleanup_duplicate_pending_reports() -> list[dict]:
    """Auto-reject pending reports whose target is already a listed scammer.

    Catches reports submitted before a duplicate check was added, and the
    race where a second pending report for the same target outlives the
    first one's approval. Returns the rows that were rejected.
    """
    pool = await _get_pool()
    rows = await pool.fetch(
        """
        UPDATE reports r
        SET status = 'rejected'
        WHERE r.status = 'pending'
          AND (
            (r.target_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM scammers s WHERE s.telegram_id = r.target_id
            ))
            OR
            (r.target_username IS NOT NULL AND EXISTS (
                SELECT 1 FROM scammers s WHERE LOWER(s.username) = LOWER(r.target_username)
            ))
          )
        RETURNING r.id, r.target_id, r.target_username
        """
    )
    return _rows(rows)


# ── Bot Groups ────────────────────────────────────────────────────────────────

async def upsert_bot_group(group_id: int, title: Optional[str]) -> None:
    pool = await _get_pool()
    await pool.execute(
        """INSERT INTO bot_groups (group_id, title, active)
           VALUES ($1, $2, TRUE)
           ON CONFLICT (group_id) DO UPDATE
             SET title = COALESCE(EXCLUDED.title, bot_groups.title),
                 active = TRUE""",
        group_id, title,
    )


async def deactivate_bot_group(group_id: int) -> None:
    pool = await _get_pool()
    await pool.execute("UPDATE bot_groups SET active = FALSE WHERE group_id = $1", group_id)


async def list_active_bot_groups() -> list[dict]:
    pool = await _get_pool()
    return _rows(await pool.fetch("SELECT * FROM bot_groups WHERE active = TRUE"))


# ── Trusted Reporters ─────────────────────────────────────────────────────────

async def add_trusted_reporter(user_id: int, username: Optional[str], added_by: int) -> None:
    pool = await _get_pool()
    await pool.execute(
        """INSERT INTO trusted_reporters (user_id, username, added_by)
           VALUES ($1, $2, $3)
           ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username""",
        user_id, username, added_by,
    )


async def remove_trusted_reporter(user_id: int) -> bool:
    pool = await _get_pool()
    result = await pool.execute("DELETE FROM trusted_reporters WHERE user_id = $1", user_id)
    return result.endswith("1")


async def list_trusted_reporters() -> list[dict]:
    pool = await _get_pool()
    return _rows(await pool.fetch("SELECT * FROM trusted_reporters ORDER BY added_at ASC"))


async def is_trusted_reporter(user_id: int) -> bool:
    pool = await _get_pool()
    row = await pool.fetchrow("SELECT 1 FROM trusted_reporters WHERE user_id = $1", user_id)
    return row is not None


# ── Weekly digest stats ───────────────────────────────────────────────────────

async def get_weekly_stats() -> dict:
    pool = await _get_pool()
    total      = await pool.fetchval("SELECT COUNT(*) FROM scammers")
    new_week   = await pool.fetchval(
        "SELECT COUNT(*) FROM scammers WHERE added_at > NOW() - INTERVAL '7 days'"
    )
    rep_total  = await pool.fetchval("SELECT COUNT(*) FROM reports")
    rep_week   = await pool.fetchval(
        "SELECT COUNT(*) FROM reports WHERE reported_at > NOW() - INTERVAL '7 days'"
    )
    approved_w = await pool.fetchval(
        "SELECT COUNT(*) FROM reports WHERE status='approved' AND reported_at > NOW() - INTERVAL '7 days'"
    )
    # Top 5 reporters this week
    top = await pool.fetch(
        """SELECT reporter_username, reporter_id, COUNT(*) AS cnt
           FROM reports
           WHERE reported_at > NOW() - INTERVAL '7 days'
           GROUP BY reporter_username, reporter_id
           ORDER BY cnt DESC LIMIT 5"""
    )
    return {
        "total_scammers": total,
        "new_this_week":  new_week,
        "reports_total":  rep_total,
        "reports_week":   rep_week,
        "approved_week":  approved_w,
        "top_reporters":  [dict(r) for r in top],
    }


# ── Appeals ───────────────────────────────────────────────────────────────────

async def add_appeal(
    *, scammer_id: int, telegram_id: Optional[int], username: Optional[str], message: str
) -> int:
    pool = await _get_pool()
    row = await pool.fetchrow(
        """INSERT INTO appeals (scammer_id, telegram_id, username, message)
           VALUES ($1, $2, $3, $4)
           RETURNING id""",
        scammer_id, telegram_id, username, message,
    )
    return row["id"]


async def get_appeal(appeal_id: int) -> Optional[dict]:
    pool = await _get_pool()
    return _row(await pool.fetchrow("SELECT * FROM appeals WHERE id = $1", appeal_id))


async def get_pending_appeal_for_scammer(scammer_id: int) -> Optional[dict]:
    pool = await _get_pool()
    return _row(await pool.fetchrow(
        "SELECT * FROM appeals WHERE scammer_id = $1 AND status = 'pending' LIMIT 1",
        scammer_id,
    ))


async def update_appeal_status(appeal_id: int, status: str) -> None:
    pool = await _get_pool()
    await pool.execute("UPDATE appeals SET status = $1 WHERE id = $2", status, appeal_id)


# ── Required Channels (force-join) ─────────────────────────────────────────────

async def add_required_channel(
    *, chat_id: Optional[int], username: Optional[str], title: Optional[str], invite_link: str
) -> int:
    pool = await _get_pool()
    row = await pool.fetchrow(
        """INSERT INTO required_channels (chat_id, username, title, invite_link)
           VALUES ($1, $2, $3, $4)
           RETURNING id""",
        chat_id, username, title, invite_link,
    )
    return row["id"]


async def list_required_channels() -> list[dict]:
    pool = await _get_pool()
    return _rows(await pool.fetch("SELECT * FROM required_channels ORDER BY id ASC"))


async def count_required_channels() -> int:
    pool = await _get_pool()
    return await pool.fetchval("SELECT COUNT(*) FROM required_channels")


async def get_required_channel_by_seq(seq: int) -> Optional[dict]:
    """Get required channel by 1-based sequential position as shown in /listchannels."""
    pool = await _get_pool()
    return _row(await pool.fetchrow(
        "SELECT * FROM required_channels ORDER BY id ASC LIMIT 1 OFFSET $1",
        seq - 1,
    ))


async def remove_required_channel(channel_id: int) -> bool:
    pool = await _get_pool()
    result = await pool.execute("DELETE FROM required_channels WHERE id = $1", channel_id)
    return result.endswith("1")


# ── Bot Admins (owner-managed, in addition to ADMIN_IDS env var) ──────────────

async def add_admin(telegram_id: int, added_by: int) -> None:
    pool = await _get_pool()
    await pool.execute(
        """INSERT INTO bot_admins (telegram_id, added_by)
           VALUES ($1, $2)
           ON CONFLICT (telegram_id) DO NOTHING""",
        telegram_id, added_by,
    )


async def remove_admin(telegram_id: int) -> bool:
    pool = await _get_pool()
    result = await pool.execute("DELETE FROM bot_admins WHERE telegram_id = $1", telegram_id)
    return result.endswith("1")


async def list_admins() -> list[dict]:
    pool = await _get_pool()
    return _rows(await pool.fetch("SELECT * FROM bot_admins ORDER BY added_at ASC"))


# ── Bot Users (track who has /start'd the bot, for "new user" notifications) ──

async def upsert_bot_user(telegram_id: int, username: Optional[str], full_name: Optional[str]) -> bool:
    """Record that telegram_id has used the bot.

    Returns True the FIRST time we see this telegram_id (a genuinely new
    user), and False on every subsequent call (returning user) — so callers
    can avoid re-sending "new user" notifications for the same person.
    """
    pool = await _get_pool()
    row = await pool.fetchrow(_UPSERT_BOT_USER_SQL + " RETURNING (xmax = 0) AS is_new",
                              telegram_id, username, full_name)
    return bool(row["is_new"])


# Shared upsert — keeps username/name current AND appends the OLD username to
# username_history whenever it changes (so we accumulate every alias we've
# ever seen for this id). Used by both passive tracking and the userbot
# harvester.
_UPSERT_BOT_USER_SQL = """
    INSERT INTO bot_users (telegram_id, username, full_name)
    VALUES ($1, $2, $3)
    ON CONFLICT (telegram_id) DO UPDATE SET
        username  = EXCLUDED.username,
        full_name = COALESCE(EXCLUDED.full_name, bot_users.full_name),
        last_seen = NOW(),
        username_history = CASE
            WHEN bot_users.username IS DISTINCT FROM EXCLUDED.username
                 AND bot_users.username IS NOT NULL
                 AND NOT (bot_users.username = ANY(COALESCE(bot_users.username_history, '{}')))
            THEN array_append(COALESCE(bot_users.username_history, '{}'), bot_users.username)
            ELSE bot_users.username_history
        END
"""


async def upsert_bot_users_bulk(rows: list[tuple]) -> int:
    """Bulk upsert (telegram_id, username, full_name) tuples — for the userbot
    harvester dumping whole member lists. Tracks username history like the
    single upsert. Returns the number of rows processed.
    """
    if not rows:
        return 0
    pool = await _get_pool()
    await pool.executemany(_UPSERT_BOT_USER_SQL, rows)
    return len(rows)


async def get_bot_user(telegram_id: int) -> Optional[dict]:
    """Look up a passively-cached (telegram_id, username, full_name).

    Populated whenever the bot sees this user — they /start the bot, send a
    message in any group the bot is in, or join such a group. Used as a
    fallback when get_chat() can't reach a user directly (e.g. /addid for
    someone who never DM'd the bot but is active in a shared group).
    """
    pool = await _get_pool()
    return _row(await pool.fetchrow(
        "SELECT * FROM bot_users WHERE telegram_id = $1", telegram_id
    ))


async def get_bot_user_by_username(username: str) -> Optional[dict]:
    """Look up a passively-cached bot_user by @username (case-insensitive).

    Telegram usernames are unique and case-insensitive, so this lets us map
    an @username back to a telegram_id using whatever the bot has seen in
    group activity — e.g. to check whether a reported @username actually
    belongs to a bot admin/owner.
    """
    pool = await _get_pool()
    return _row(await pool.fetchrow(
        "SELECT * FROM bot_users WHERE LOWER(username) = LOWER($1)",
        username.lstrip("@"),
    ))


# ── Admin action audit log ────────────────────────────────────────────────────

async def log_admin_action(
    actor_id: Optional[int],
    actor_username: Optional[str],
    action: str,
    target_type: Optional[str] = None,
    target_id=None,
    detail: Optional[str] = None,
) -> None:
    """Record one admin action. Never raises — auditing must not break the
    action it's logging."""
    try:
        pool = await _get_pool()
        await pool.execute(
            "INSERT INTO admin_actions "
            "(actor_id, actor_username, action, target_type, target_id, detail) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            actor_id, actor_username, action, target_type,
            (str(target_id) if target_id is not None else None), detail,
        )
    except Exception as e:
        logger.warning("log_admin_action failed: %s", e)


async def recent_admin_actions(limit: int = 200) -> list[dict]:
    """Most recent admin actions, newest first."""
    pool = await _get_pool()
    return _rows(await pool.fetch(
        "SELECT * FROM admin_actions ORDER BY created_at DESC LIMIT $1", limit
    ))


async def admin_action_counts() -> list[dict]:
    """Per-admin action totals (for the web panel's at-a-glance view)."""
    pool = await _get_pool()
    return _rows(await pool.fetch(
        "SELECT actor_id, "
        "       MAX(actor_username) AS actor_username, "
        "       COUNT(*)            AS total, "
        "       MAX(created_at)     AS last_action "
        "FROM admin_actions "
        "GROUP BY actor_id "
        "ORDER BY total DESC"
    ))
