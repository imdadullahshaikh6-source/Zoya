"""Pin / unpin messages in groups.

Commands:
  .pin        (reply)  — silently pin the replied message
  .unpin      (reply)  — unpin the replied message
  .unpin               — unpin the last pinned message
  .unpinall            — unpin all pinned messages

Cross-verification: only admins with `can_pin_messages` (or the owner) can use
these — AND the bot itself must also have `can_pin_messages`. If either side
lacks the permission, the command refuses with a clear message."""
import logging

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database as dbase
from common import T, dual_command, say

log = logging.getLogger("pin")

HELP_TXT = (
    "<b>📌 𝙋𝙞𝙣 / 𝙐𝙣𝙥𝙞𝙣</b>\n\n"
    "• <code>/pin</code> (or <code>.pin</code>) (reply) — silently pin the replied message.\n"
    "• <code>/unpin</code> (or <code>.unpin</code>) (reply) — unpin the replied message.\n"
    "• <code>/unpin</code> (or <code>.unpin</code>) — unpin the last pinned message.\n"
    "• <code>/unpinall</code> (or <code>.unpinall</code>) — unpin all pinned messages in this chat.\n\n"
    "<i>Only admins with <b>Pin Messages</b> permission can use these. "
    "Bot also needs Pin Messages permission.</i>"
)
COMMANDS = [
    ("pin", "Pin a replied message"),
    ("unpin", "Unpin a message"),
    ("unpinall", "Unpin all messages"),
]


async def _user_can_pin(ctx, chat_id: int, user_id: int) -> bool:
    """True only if the user is the owner or an admin with can_pin_messages."""
    try:
        m = await ctx.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    if m.status == ChatMemberStatus.OWNER:
        return True
    if m.status != ChatMemberStatus.ADMINISTRATOR:
        return False
    return bool(getattr(m, "can_pin_messages", False))


async def _bot_can_pin(ctx, chat_id: int) -> bool:
    """True if the bot is an admin with can_pin_messages in this chat."""
    me = ctx.application.bot_data.get("me")
    if not me:
        return False
    try:
        m = await ctx.bot.get_chat_member(chat_id, me.id)
    except TelegramError:
        return False
    return (m.status == ChatMemberStatus.ADMINISTRATOR
            and bool(getattr(m, "can_pin_messages", False)))


async def _guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Shared permission check. Sends the right refusal and returns False
    if either the user or the bot lacks pin permission."""
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return False
    if not await _user_can_pin(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("⚠️ you need <b>Pin Messages</b> permission to use this."),
                  reply_to=msg.message_id)
        return False
    if not await _bot_can_pin(ctx, chat.id):
        await say(ctx, chat.id, T("⚠️ give me <b>Pin Messages</b> permission first."),
                  reply_to=msg.message_id)
        return False
    return True


# ───────────── commands ─────────────

async def pin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not await _guard(update, ctx):
        return
    target = msg.reply_to_message
    if not target:
        await say(ctx, chat.id, T("reply to a message with <code>.pin</code>."), reply_to=msg.message_id)
        return
    try:
        await ctx.bot.pin_chat_message(chat.id, target.message_id, disable_notification=True)
        await say(ctx, chat.id, T("📌 pinned."), reply_to=msg.message_id)
    except TelegramError as e:
        await say(ctx, chat.id, T(f"❌ couldn't pin: {e}"), reply_to=msg.message_id)


async def unpin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not await _guard(update, ctx):
        return
    target = msg.reply_to_message
    try:
        if target:
            await ctx.bot.unpin_chat_message(chat.id, target.message_id)
        else:
            await ctx.bot.unpin_chat_message(chat.id)
        await say(ctx, chat.id, T("📌 unpinned."), reply_to=msg.message_id)
    except TelegramError as e:
        await say(ctx, chat.id, T(f"❌ couldn't unpin: {e}"), reply_to=msg.message_id)


async def unpinall_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not await _guard(update, ctx):
        return
    try:
        await ctx.bot.unpin_all_chat_messages(chat.id)
        await say(ctx, chat.id, T("📌 all messages unpinned."), reply_to=msg.message_id)
    except TelegramError as e:
        await say(ctx, chat.id, T(f"❌ couldn't unpin all: {e}"), reply_to=msg.message_id)


def register(app):
    dual_command(app, "pin", pin_cmd)
    dual_command(app, "unpin", unpin_cmd)
    dual_command(app, "unpinall", unpinall_cmd)
  
