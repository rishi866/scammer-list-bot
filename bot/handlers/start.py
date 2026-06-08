from telegram import Update
from telegram.ext import ContextTypes

WELCOME = """
<b>Scammer List Bot</b>

Report and verify Telegram scammers to keep the community safe.

<b>Commands:</b>
/check — Check if someone is a known scammer
/report — Report a suspected scammer
/help — Show this message

Admins can use /add, /remove, /list, /pending, /approve, /reject, /stats.
"""


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode="HTML")
