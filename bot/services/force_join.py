"""Force-join membership checks against admin-configured channels/groups.

Used by bot.handlers.force_join — kept separate so the Telegram-API-facing
membership check and prompt-building logic can be tested/reused without
pulling in the handler/registration plumbing.
"""
from __future__ import annotations

import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from bot.db import list_required_channels
from bot.services.emoji_fx import em

logger = logging.getLogger(__name__)

# ChatMember.status values that mean "definitely not in the chat"
_NOT_JOINED = {"left", "kicked"}


async def get_unjoined_channels(bot: Bot, user_id: int) -> list[dict]:
    """Return the configured channels/groups `user_id` has NOT joined.

    A channel/group the bot itself can't check (not a member/admin there,
    chat not found, etc.) is silently skipped with a warning — a single
    misconfigured entry should never lock everyone out of the bot.
    """
    channels = await list_required_channels()
    if not channels:
        return []

    pending: list[dict] = []
    for ch in channels:
        chat_ref = ch.get("chat_id") or (f"@{ch['username']}" if ch.get("username") else None)
        if not chat_ref:
            continue
        try:
            member = await bot.get_chat_member(chat_ref, user_id)
        except TelegramError as exc:
            logger.warning("force_join: could not check user %s in %s: %s", user_id, chat_ref, exc)
            continue

        if member.status in _NOT_JOINED:
            pending.append(ch)
        elif member.status == "restricted" and not getattr(member, "is_member", True):
            pending.append(ch)

    return pending


def build_join_prompt(channels: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    """Return (HTML text, keyboard) prompting the user to join `channels`."""
    names = "\n".join(f"• {c.get('title') or c.get('username') or 'Channel'}" for c in channels)
    text = (
        em(
            "🔒 <b>Join to Continue</b>\n\n"
            "Please join the following channel(s)/group(s) to use this bot:\n\n"
        )
        + names
        + em("\n\nAfter joining, tap <b>✅ I've Joined</b> below.")
    )

    buttons = [
        [InlineKeyboardButton(
            f"➕ Join {c.get('title') or c.get('username') or 'Channel'}",
            url=c["invite_link"],
        )]
        for c in channels
    ]
    buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join")])
    return text, InlineKeyboardMarkup(buttons)
