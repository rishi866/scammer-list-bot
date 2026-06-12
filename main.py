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
from bot.handlers.appeal        import register as register_appeal_handlers
from bot.handlers.force_join   import (
    force_join_guard,
    addchannel_command,
    removechannel_command,
    listchannels_command,
    groupid_command,
)
from bot.handlers.callbacks    import callback_router
from bot.handlers.emoji_admin  import setemoji_cmd, delemoji_cmd, listemoji_cmd, loadpack_cmd, extractmoji_cmd
from bot.handlers.trusted      import addtrusted_cmd, removetrusted_cmd, listtrusted_cmd
from bot.handlers.owner        import addadmin_command, removeadmin_command, listadmins_command
from bot.handlers.new_member   import on_new_member
from bot.handlers.admin        import (
    build_add_handler,
    remove_command,
    edit_command,
    list_command,
    pending_command,
    approve_command,
    reject_command,
    stats_command,
    fixids_command,
    setid_command,
    refreshusername_command,
)
from bot.services.admins             import refresh_admin_cache
from bot.services.username_refresher import username_refresh_loop
from bot.services.broadcaster        import on_bot_member_update
from bot.services.weekly_digest      import weekly_digest_loop
from bot.services.user_tracker       import track_message_user, track_member_join
from bot.services.user_sync          import user_sync_loop

BOT_TOKEN = os.getenv("BOT_TOKEN", "")


async def run() -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set — exiting")
        return

    await init_db()
    await refresh_admin_cache()

    try:
        await emoji_fx.load()
        logger.info("emoji_fx ready")
    except Exception as e:
        logger.warning("emoji_fx load failed (non-fatal): %s", e)

    req = HTTPXRequest(connect_timeout=10, read_timeout=15, write_timeout=20, pool_timeout=15)
    app = Application.builder().token(BOT_TOKEN).request(req).build()

    # ── Force-join gate (group=-1 → runs before EVERYTHING else) ──────────────
    # No-op until an admin runs /addchannel. See bot/handlers/force_join.py
    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.PRIVATE, force_join_guard), group=-1)
    app.add_handler(CallbackQueryHandler(force_join_guard), group=-1)

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

    # /appeal in PM → dispute a listing (notifies admins with approve/reject buttons)
    register_appeal_handlers(app)

    # Inline button callbacks (approve/reject/severity + scammer_list pagination + quick actions)
    app.add_handler(CallbackQueryHandler(callback_router))

    # ── Admin-only (private chat) ─────────────────────────────────────────────
    app.add_handler(build_add_handler())   # multi-step /add in PM → direct DB insert
    app.add_handler(CommandHandler("remove",        remove_command))
    app.add_handler(CommandHandler("edit",          edit_command))
    app.add_handler(CommandHandler("list",          list_command))
    app.add_handler(CommandHandler("pending",       pending_command))
    app.add_handler(CommandHandler("approve",       approve_command))
    app.add_handler(CommandHandler("reject",        reject_command))
    app.add_handler(CommandHandler("stats",         stats_command))
    app.add_handler(CommandHandler("fixids",        fixids_command))
    app.add_handler(CommandHandler("setid",         setid_command))
    app.add_handler(CommandHandler("refreshusername", refreshusername_command))
    # /addid is registered in register_forward_handlers (handles both admin+user)

    app.add_handler(CommandHandler("addtrusted",    addtrusted_cmd))
    app.add_handler(CommandHandler("removetrusted", removetrusted_cmd))
    app.add_handler(CommandHandler("listtrusted",   listtrusted_cmd))

    # Owner-only admin management
    app.add_handler(CommandHandler("addadmin",    addadmin_command))
    app.add_handler(CommandHandler("removeadmin", removeadmin_command))
    app.add_handler(CommandHandler("listadmins",  listadmins_command))

    # Force-join channel/group management (admin)
    app.add_handler(CommandHandler("addchannel",    addchannel_command))
    app.add_handler(CommandHandler("removechannel", removechannel_command))
    app.add_handler(CommandHandler("listchannels",  listchannels_command))
    app.add_handler(CommandHandler("groupid",       groupid_command))

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

    # ── Passive "seen users" cache (low priority, never blocks anything) ──────
    # Builds bot_users from normal group activity — fallback for /addid,
    # /refreshusername etc. when get_chat() can't reach a user directly.
    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, track_message_user), group=10)
    app.add_handler(ChatMemberHandler(track_member_join, ChatMemberHandler.CHAT_MEMBER), group=10)

    # ── Bot command menus ─────────────────────────────────────────────────────
    group_cmds = [
        BotCommand("add",          "Report scammer: /add @username reason"),
        BotCommand("addid",        "Report by Telegram ID: /addid <id> reason"),
        BotCommand("report",       "Report scammer: /report @username reason"),
        BotCommand("check",        "Check if someone is a known scammer"),
        BotCommand("scammer_list", "View all confirmed scammers"),
    ]
    private_cmds = [
        BotCommand("start",         "Welcome"),
        BotCommand("check",         "Check if someone is a scammer"),
        BotCommand("scammer_list",  "View all confirmed scammers"),
        BotCommand("report",        "Report a suspected scammer"),
        BotCommand("addid",         "Report by Telegram ID: /addid <id> reason"),
        BotCommand("appeal",        "Dispute your listing (if added by mistake)"),
        BotCommand("help",          "Show help"),
        BotCommand("addtrusted",    "Add trusted reporter (admin)"),
        BotCommand("removetrusted", "Remove trusted reporter (admin)"),
        BotCommand("listtrusted",   "List trusted reporters (admin)"),
        BotCommand("listadmins",    "List bot admins (admin)"),
    ]

    await app.initialize()
    await app.start()
    try:
        await app.bot.set_my_commands(group_cmds,   scope=BotCommandScopeAllGroupChats())
        await app.bot.set_my_commands(private_cmds, scope=BotCommandScopeAllPrivateChats())
    except Exception as e:
        logger.warning("set_my_commands failed: %s", e)

    await app.updater.start_polling(
        drop_pending_updates  = True,
        allowed_updates       = ["message", "callback_query", "chat_member", "my_chat_member"],
        poll_interval         = 2.0,   # poll every 2s instead of continuously
        timeout               = 20,    # long-polling timeout
    )
    logger.info("Scammer List Bot is live.")

    # Background tasks
    refresh_task = asyncio.create_task(username_refresh_loop(app.bot), name="username-refresh")
    digest_task  = asyncio.create_task(weekly_digest_loop(app.bot),   name="weekly-digest")
    sync_task    = asyncio.create_task(user_sync_loop(),              name="user-sync")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        for task in (refresh_task, digest_task, sync_task):
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
