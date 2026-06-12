"""Userbot member-list harvester.

WHAT THIS IS
------------
A read-only "directory builder". The Telegram *Bot* API cannot enumerate a
group's members, and cannot resolve an arbitrary numeric user-id to a
username. A regular *user* account (driven here via Telethon) can read full
member lists, so we run one purely to scrape {id, username, name} for every
member of every group/channel the account is in, and store it in the shared
`bot_users` table.

The main bot then uses that table as a fallback in /addid and
/refreshusername — so an admin can enter a scammer's numeric id and still get
their username/name, even if that scammer never started the bot and is no
longer in any group the bot itself is in.

IMPORTANT
---------
* This account NEVER sends messages, adds members, or does anything writeable
  on Telegram — it only reads member lists. That keeps the ban-risk low.
* It still needs a real phone number / API credentials. See userbot/README.md.
* First run must be interactive (to enter the phone number + login code, which
  creates the session file). After that it can run head-less under systemd.

ENV (see .env.example)
----------------------
  USERBOT_API_ID              from https://my.telegram.org
  USERBOT_API_HASH            from https://my.telegram.org
  USERBOT_SESSION             session file name (default: userbot)
  USERBOT_SCRAPE_INTERVAL_HOURS   how often to re-scan everything (default 12)
  USERBOT_PER_REQUEST_DELAY   seconds to wait between groups (default 3)
  DATABASE_URL                same Postgres as the bot
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("userbot")

from telethon import TelegramClient
from telethon.errors import (
    ChatAdminRequiredError,
    FloodWaitError,
    ChannelPrivateError,
)
from telethon.tl.types import User

from bot.db import init_db, upsert_bot_users_bulk

API_ID        = int(os.getenv("USERBOT_API_ID", "0") or "0")
API_HASH      = os.getenv("USERBOT_API_HASH", "")
SESSION       = os.getenv("USERBOT_SESSION", "userbot")
INTERVAL_HRS  = float(os.getenv("USERBOT_SCRAPE_INTERVAL_HOURS", "12"))
GROUP_DELAY   = float(os.getenv("USERBOT_PER_REQUEST_DELAY", "3"))

# Flush to DB every this-many members (keeps memory flat on huge groups).
DB_BATCH = 500


async def _scrape_dialog(client: TelegramClient, dialog) -> int:
    """Scrape one group/channel's member list into bot_users. Returns count."""
    batch: list[tuple] = []
    saved = 0
    try:
        async for member in client.iter_participants(dialog.entity):
            if not isinstance(member, User) or member.bot or member.deleted:
                continue
            full = " ".join(filter(None, [member.first_name, member.last_name])) or None
            batch.append((member.id, member.username, full))
            if len(batch) >= DB_BATCH:
                saved += await upsert_bot_users_bulk(batch)
                batch.clear()
    except FloodWaitError as e:
        logger.warning("FloodWait %ds while scraping '%s' — sleeping", e.seconds, getattr(dialog, "name", "?"))
        await asyncio.sleep(e.seconds + 5)
    except (ChatAdminRequiredError, ChannelPrivateError) as e:
        logger.info("Skipping '%s' (members not readable): %s", getattr(dialog, "name", "?"), e)
    except Exception as e:
        logger.warning("Error scraping '%s': %s", getattr(dialog, "name", "?"), e)

    if batch:
        saved += await upsert_bot_users_bulk(batch)
    return saved


async def harvest_once(client: TelegramClient) -> None:
    """One full pass over every group/channel the account belongs to."""
    groups = 0
    members = 0
    async for dialog in client.iter_dialogs():
        if not (dialog.is_group or dialog.is_channel):
            continue
        n = await _scrape_dialog(client, dialog)
        if n:
            groups += 1
            members += n
            logger.info("  '%s': cached %d members", getattr(dialog, "name", "?"), n)
        await asyncio.sleep(GROUP_DELAY)   # be gentle — avoid rate limits
    logger.info("Harvest pass done: %d members across %d group(s)", members, groups)


async def main() -> None:
    if not API_ID or not API_HASH:
        logger.error(
            "USERBOT_API_ID / USERBOT_API_HASH not set in .env. "
            "Get them from https://my.telegram.org — see userbot/README.md"
        )
        return

    await init_db()

    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        if sys.stdin.isatty():
            # Interactive first-run: prompts for phone number + login code,
            # then writes <SESSION>.session so future runs are head-less.
            logger.info("First-time login — follow the prompts below.")
            await client.start()
        else:
            logger.error(
                "Userbot is not logged in yet and there's no terminal to log in "
                "with. Run this once in a shell:  python -m userbot.harvester  "
                "(enter the phone number + code), then start the service."
            )
            await client.disconnect()
            return

    me = await client.get_me()
    logger.info("Userbot logged in as %s (id=%s)", me.username or me.first_name, me.id)

    while True:
        try:
            await harvest_once(client)
        except FloodWaitError as e:
            logger.warning("FloodWait %ds — sleeping", e.seconds)
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            logger.error("Harvest loop error: %s", e)
        logger.info("Next harvest in %.1f h", INTERVAL_HRS)
        await asyncio.sleep(INTERVAL_HRS * 3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
