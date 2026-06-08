from telegram import Update
from telegram.ext import ContextTypes
from bot.services.emoji_fx import em

_WELCOME = (
    "✨ <b>Scammer List Bot</b>\n\n"
    "Report and verify Telegram scammers to keep the community safe.\n\n"
    "<b>Commands:</b>\n"
    "🔍 /check — Check if someone is a known scammer\n"
    "📝 /add @username — Submit a scammer (in group)\n"
    "📋 /scammer_list — View all confirmed scammers\n"
    "📨 /report — Report via private chat\n"
    "ℹ️ /help — Show this message"
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(em(_WELCOME), parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(em(_WELCOME), parse_mode="HTML")
