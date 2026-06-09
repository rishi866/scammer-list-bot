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

from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ChatMemberHandler, MessageHandler, filters,
)
from telegram.request import HTTPXRequest

from bot.db import init_db
from bot.services import emoji_fx
from bot.handlers.start        import start_command, help_command
from bot.handlers.check        import check_command
from bot.handlers.group_add    import group_add_command
from bot.handlers.scammer_list import scammer_list_command
from bot.handlers.report       import build_report_handler
from bot.handlers.report_forward import register as register_forward_handlers
from bot.handlers.callbacks    import callback_router
from bot.handlers.emoji_admin  import setemoji_cmd, delemoji_cmd, listemoji_cmd, loadpack_cmd, extractmoji_cmd
from bot.handlers.trusted      import addtrusted_cmd, removetrusted_cmd, listtrusted_cmd
from bot.handlers.new_member   import on_new_member
from bot.handlers.admin        import (
    build_add_handler,
    remove_command,
    list_command,
    pending_command,
    approve_command,
    reject_command,
    stats_command,
    fixids_command,
    setid_command,
    addid_command,
)
from bot.services.username_refresher import username_refresh_loop
from bot.services.broadcaster        import on_bot_member_update
from bot.services.weekly_digest      import weekly_digest_loop

BOT_TOKEN = os.getenv("BOT_TOKEN", "")


async def run() -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set — exiting")
        return

    await init_db()

    try:
        await emoji_fx.load()
        logger.info("emoji_fx ready")
    except Exception as e:
        logger.warning("emoji_fx load failed (non-fatal): %s", e)

    req = HTTPXRequest(connect_timeout=10, read_timeout=15, write_timeout=20, pool_timeout=15)
    app = Application.builder().token(BOT_TOKEN).request(req).build()

    # ── Public handlers (group + private) ────────────────────────────────────
    app.add_handler(CommandHandler("start",        start_command))
    app.add_handler(CommandHandler("help",         help_command))
    app.add_handler(CommandHandler("check",        check_command))
    app.add_handler(CommandHandler("scammer_list", scammer_list_command))

    # /add in GROUP → submission flow (any member → admin approval)
    app.add_handler(CommandHandler("add", group_add_command, filters=filters.ChatType.GROUPS))

    # /report in private → multi-step (goes to approval)
    app.add_handler(build_report_handler())

    # Forward any message in PM → unified handler (admin: resolve/quick-add, user: report flow)
    register_forward_handlers(app)

    # Inline button callbacks (approve/reject/severity + scammer_list pagination)
    app.add_handler(CallbackQueryHandler(callback_router))

    # ── Admin-only (private chat) ─────────────────────────────────────────────
    app.add_handler(build_add_handler())   # multi-step /add in PM → direct DB insert
    app.add_handler(CommandHandler("remove",        remove_command))
    app.add_handler(CommandHandler("list",          list_command))
    app.add_handler(CommandHandler("pending",       pending_command))
    app.add_handler(CommandHandler("approve",       approve_command))
    app.add_handler(CommandHandler("reject",        reject_command))
    app.add_handler(CommandHandler("stats",         stats_command))
    app.add_handler(CommandHandler("fixids",        fixids_command))
    app.add_handler(CommandHandler("setid",         setid_command))
    app.add_handler(CommandHandler("addid",         addid_command))

    app.add_handler(CommandHandler("addtrusted",    addtrusted_cmd))
    app.add_handler(CommandHandler("removetrusted", removetrusted_cmd))
    app.add_handler(CommandHandler("listtrusted",   listtrusted_cmd))

    # ── Emoji admin commands ───────────────────────────────────────────────────
    app.add_handler(CommandHandler("setemoji",    setemoji_cmd))
    app.add_handler(CommandHandler("delemoji",    delemoji_cmd))
    app.add_handler(CommandHandler("listemoji",   listemoji_cmd))
    app.add_handler(CommandHandler("loadpack",    loadpack_cmd))
    app.add_handler(CommandHandler("extractmoji", extractmoji_cmd))

    # ── Group membership tracking ─────────────────────────────────────────────
    # Track which groups the bot is in (for cross-group broadcast)
    app.add_handler(ChatMemberHandler(on_bot_member_update, ChatMemberHandler.MY_CHAT_MEMBER))

    # Auto-check new members against scammer list
    app.add_handler(ChatMemberHandler(on_new_member, ChatMemberHandler.CHAT_MEMBER))

    # ── Bot command menus ─────────────────────────────────────────────────────
    group_cmds = [
        BotCommand("add",          "Submit a scammer for admin review"),
        BotCommand("check",        "Check if someone is a known scammer"),
        BotCommand("scammer_list", "View all confirmed scammers"),
    ]
    private_cmds = [
        BotCommand("start",         "Welcome"),
        BotCommand("check",         "Check if someone is a scammer"),
        BotCommand("scammer_list",  "View all confirmed scammers"),
        BotCommand("report",        "Report a suspected scammer"),
        BotCommand("help",          "Show help"),
        BotCommand("addtrusted",    "Add trusted reporter (admin)"),
        BotCommand("removetrusted", "Remove trusted reporter (admin)"),
        BotCommand("listtrusted",   "List trusted reporters (admin)"),
    ]

    await app.initialize()
    await app.start()
    try:
        await app.bot.set_my_commands(group_cmds,   scope=BotCommandScopeAllGroupChats())
        await app.bot.set_my_commands(private_cmds, scope=BotCommandScopeAllPrivateChats())
    except Exception as e:
        logger.warning("set_my_commands failed: %s", e)

    await app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"])
    logger.info("Scammer List Bot is live.")

    # Background tasks
    refresh_task = asyncio.create_task(username_refresh_loop(app.bot), name="username-refresh")
    digest_task  = asyncio.create_task(weekly_digest_loop(app.bot),   name="weekly-digest")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        for task in (refresh_task, digest_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(run())
