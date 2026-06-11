"""Passive "seen users" cache.

Builds up bot_users (telegram_id -> username, full_name) just by watching
normal group activity the bot already receives — no extra API calls, no
special access. Same principle big bots like Rose/Sagarmata rely on: being
present in many groups and quietly noting who's who as messages/joins go by.

This gives /addid, /refreshusername etc. a fallback for users who've never
DM'd the bot directly but ARE active in a group the bot is in.

Both handlers below are registered at a high group number (low priority) and
never raise/return anything that could interfere with other handlers — they
just observe and cache.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from bot.db import upsert_bot_user

logger = logging.getLogger(__name__)


async def track_message_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cache (id, username, name) for whoever sent this group message."""
    user = update.effective_user
    if not user or user.is_bot:
        return

    full_name = " ".join(filter(None, [user.first_name, user.last_name])) or None
    try:
        await upsert_bot_user(user.id, user.username, full_name)
    except Exception as e:  # never let caching break real handlers
        logger.debug("user_tracker (message) failed for %s: %s", user.id, e)


async def track_member_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cache (id, username, name) for anyone whose membership status changes."""
    event = update.chat_member
    if not event:
        return

    user = event.new_chat_member.user
    if user.is_bot:
        return

    full_name = " ".join(filter(None, [user.first_name, user.last_name])) or None
    try:
        await upsert_bot_user(user.id, user.username, full_name)
    except Exception as e:
        logger.debug("user_tracker (join) failed for %s: %s", user.id, e)
