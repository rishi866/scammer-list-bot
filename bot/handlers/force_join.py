"""Force-join gate — require users to join configured channel(s)/group(s)
before using the bot in private chat, plus admin commands to manage the list.

Admin commands:
  /addchannel @username                       — add a public channel/group
  /addchannel <chat_id> <invite_link> [title] — add a private channel/group
  /removechannel <#>                          — remove (see /listchannels)
  /listchannels                               — show configured channels/groups

How it works:
  force_join_guard is registered in handler group=-1, so it runs before every
  other handler. In private chats, if the user (non-admin) hasn't joined all
  configured channels/groups, it replies with a "please join" message + join
  buttons and raises ApplicationHandlerStop so nothing else (commands, quick
  buttons, conversations, etc.) processes that update.

  The "✅ I've Joined" button (callback_data="check_join") is re-checked via
  callback_router → recheck_join_callback (kept out of the group=-1 guard so
  the recheck itself isn't blocked).

If no channels/groups are configured, get_unjoined_channels() always returns
an empty list, so this is a complete no-op — existing behaviour is unchanged
until an admin runs /addchannel.
"""
from __future__ import annotations

import logging
import os

from telegram import Update
from telegram.ext import ContextTypes, ApplicationHandlerStop
from telegram.error import TelegramError

from bot.db import (
    add_required_channel,
    list_required_channels,
    count_required_channels,
    get_required_channel_by_seq,
    remove_required_channel,
)
from bot.handlers.admin import admin_only
from bot.services.emoji_fx import em
from bot.services.force_join import get_unjoined_channels, build_join_prompt

logger = logging.getLogger(__name__)


def _admin_ids() -> set[int]:
    return {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}


# ── Gate (group=-1, runs before all other handlers) ───────────────────────────

async def force_join_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type != "private" or not user:
        return
    if user.id in _admin_ids():
        return

    query = update.callback_query
    if query and query.data == "check_join":
        return  # let callback_router → recheck_join_callback handle it

    # Cheap check first — avoids a Bot API call per message once configured list is empty
    channels = await list_required_channels()
    if not channels:
        return

    not_joined = await get_unjoined_channels(context.bot, user.id)
    if not not_joined:
        return

    text, kbd = build_join_prompt(not_joined)
    target = update.effective_message
    if target:
        try:
            await target.reply_text(text, parse_mode="HTML", reply_markup=kbd)
        except TelegramError as exc:
            logger.warning("Could not send join prompt to %s: %s", user.id, exc)

    raise ApplicationHandlerStop


# ── "✅ I've Joined" recheck (wired via callbacks.callback_router) ────────────

async def recheck_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user  = update.effective_user

    not_joined = await get_unjoined_channels(context.bot, user.id)
    if not_joined:
        await query.answer(
            "❌ You haven't joined all the required channel(s)/group(s) yet.",
            show_alert=True,
        )
        return

    await query.answer("✅ Verified! You can now use the bot.", show_alert=True)
    try:
        await query.edit_message_text(
            em("✅ <b>Verified!</b>\n\nThanks for joining. Send /start to begin."),
            parse_mode="HTML",
        )
    except TelegramError:
        pass


# ── /groupid — find a chat's ID/username for /addchannel ─────────────────────

@admin_only
async def groupid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run inside the target group/channel to get what /addchannel needs."""
    chat = update.effective_chat

    if chat.type == "private":
        await update.message.reply_text(
            em(
                "ℹ️ Send <code>/groupid</code> <b>inside</b> the group/channel you "
                "want to use for force-join (not here in PM)."
            ),
            parse_mode="HTML",
        )
        return

    if chat.username:
        hint = f"✅ Public — use:\n<code>/addchannel @{chat.username}</code>"
    else:
        hint = (
            "🔒 Private (no public username).\n"
            "Get an invite link: group/channel info → <b>Invite Links</b>, then in PM:\n"
            f"<code>/addchannel {chat.id} &lt;invite_link&gt; {chat.title}</code>"
        )

    await update.message.reply_text(
        em(
            f"🆔 <b>Chat Info</b>\n\n"
            f"📛 Title    : {chat.title}\n"
            f"🔑 Chat ID  : <code>{chat.id}</code>\n"
            f"📝 Username : {f'@{chat.username}' if chat.username else '— (private)'}\n\n"
        ) + hint,
        parse_mode="HTML",
    )


# ── Admin: /addchannel /removechannel /listchannels ───────────────────────────

_ADDCHANNEL_USAGE = (
    "🔒 <b>Usage</b>\n\n"
    "<code>/addchannel @username</code>\n"
    "→ public channel/group (bot must be a member/admin there)\n\n"
    "<code>/addchannel -100xxxxxxxxxx https://t.me/+invite [Title]</code>\n"
    "→ private channel/group\n\n"
    "<b>Examples:</b>\n"
    "<code>/addchannel @MyChannel</code>\n"
    "<code>/addchannel -1001234567890 https://t.me/+AbCdEfGh My Group</code>\n\n"
    "⚠️ The bot must already be added to that channel/group (as <b>admin</b> "
    "for channels) so it can verify membership."
)


@admin_only
async def addchannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(em(_ADDCHANNEL_USAGE), parse_mode="HTML")
        return

    first    = args[0]
    username = None

    if first.startswith("@"):
        username = first.lstrip("@")
        try:
            chat = await context.bot.get_chat(f"@{username}")
        except TelegramError as e:
            await update.message.reply_text(
                em(
                    f"❌ Could not fetch @{username}: <code>{e}</code>\n\n"
                    f"Make sure the bot has been added to that channel/group."
                ),
                parse_mode="HTML",
            )
            return

        chat_id     = chat.id
        username    = chat.username or username
        title       = chat.title or f"@{username}"
        invite_link = f"https://t.me/{username}"

    elif first.lstrip("-").isdigit():
        if len(args) < 2 or not args[1].startswith(("http://", "https://", "tg://")):
            await update.message.reply_text(em(_ADDCHANNEL_USAGE), parse_mode="HTML")
            return

        chat_id     = int(first)
        invite_link = args[1]
        title       = " ".join(args[2:]).strip() or None
        if not title:
            try:
                chat     = await context.bot.get_chat(chat_id)
                title    = chat.title
                username = chat.username
            except TelegramError:
                title = None

    else:
        await update.message.reply_text(em(_ADDCHANNEL_USAGE), parse_mode="HTML")
        return

    await add_required_channel(chat_id=chat_id, username=username, title=title, invite_link=invite_link)
    total = await count_required_channels()

    await update.message.reply_text(
        em(
            f"✅ <b>Required channel/group added (#{total}).</b>\n\n"
            f"📛 Title : {title or '—'}\n"
            f"🔗 Link  : {invite_link}\n\n"
            f"Non-admin users must now join this to use the bot in PM.\n"
            f"Manage with /listchannels and /removechannel &lt;#&gt;."
        ),
        parse_mode="HTML",
    )


@admin_only
async def removechannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            em("Usage: /removechannel &lt;#&gt;  (see /listchannels)"), parse_mode="HTML"
        )
        return

    seq   = int(args[0])
    entry = await get_required_channel_by_seq(seq)
    if not entry:
        await update.message.reply_text(em(f"❌ No entry at #{seq}."), parse_mode="HTML")
        return

    label = entry.get("title") or (f"@{entry['username']}" if entry.get("username") else "—")
    ok    = await remove_required_channel(entry["id"])
    if ok:
        await update.message.reply_text(em(f"✅ Removed <b>#{seq}</b> ({label})."), parse_mode="HTML")
    else:
        await update.message.reply_text(em(f"❌ Could not remove #{seq}."), parse_mode="HTML")


@admin_only
async def listchannels_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    channels = await list_required_channels()
    if not channels:
        await update.message.reply_text(
            em(
                "📋 <b>No required channels/groups set.</b>\n\n"
                "Anyone can use the bot freely.\n"
                "Use /addchannel to require users to join a channel/group first."
            ),
            parse_mode="HTML",
        )
        return

    lines = []
    for i, c in enumerate(channels, start=1):
        title = c.get("title") or (f"@{c['username']}" if c.get("username") else "—")
        lines.append(f"<b>{i}.</b> {title}\n   🔗 {c['invite_link']}")

    await update.message.reply_text(
        em(f"🔒 <b>Required Channels/Groups ({len(channels)})</b>\n\n")
        + "\n".join(lines)
        + em("\n\n/removechannel &lt;#&gt; to remove."),
        parse_mode="HTML",
    )
