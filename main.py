"""Scammer List Bot — main entry point."""
import asyncio
import logging
import os

from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

from telegram import BotCommand, BotCommandScopeAllPrivateChats
from telegram.ext import Application, CommandHandler, filters
from telegram.request import HTTPXRequest

from bot.db import init_db
from bot.handlers.start import start_command, help_command
from bot.handlers.check import check_command
from bot.services.username_refresher import username_refresh_loop
from bot.handlers.report import build_report_handler
from bot.handlers.admin import (
    build_add_handler,
    remove_command,
    list_command,
    pending_command,
    approve_command,
    reject_command,
    stats_command,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")


async def run() -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set — exiting")
        return

    await init_db()

    req = HTTPXRequest(connect_timeout=10, read_timeout=15, write_timeout=20, pool_timeout=15)
    app = Application.builder().token(BOT_TOKEN).request(req).build()

    # Public
    app.add_handler(CommandHandler("start",  start_command))
    app.add_handler(CommandHandler("help",   help_command))
    app.add_handler(CommandHandler("check",  check_command))
    app.add_handler(build_report_handler())

    # Admin
    app.add_handler(build_add_handler())
    app.add_handler(CommandHandler("remove",  remove_command))
    app.add_handler(CommandHandler("list",    list_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("reject",  reject_command))
    app.add_handler(CommandHandler("stats",   stats_command))

    cmds = [
        BotCommand("start",  "Welcome"),
        BotCommand("check",  "Check if someone is a scammer"),
        BotCommand("report", "Report a scammer"),
        BotCommand("help",   "Show help"),
    ]
    await app.initialize()
    await app.start()
    try:
        await app.bot.set_my_commands(cmds)
        await app.bot.set_my_commands(cmds, scope=BotCommandScopeAllPrivateChats())
    except Exception as e:
        logger.warning("set_my_commands failed: %s", e)

    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Scammer List Bot is live.")

    refresh_task = asyncio.create_task(
        username_refresh_loop(app.bot), name="username-refresh"
    )

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(run())
