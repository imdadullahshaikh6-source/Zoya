"""Pin / unpin messages in groups.

Commands:
  .pin        (reply)  — silently pin the replied message
  .unpin      (reply)  — unpin the replied message
  .unpin               — unpin the last pinned message
  .unpinall            — unpin all pinned messages

Only full admins can use these. The bot needs "Pin Messages" permission."""
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
    "• <code>.pin</code> (reply) — silently pin the replied message.\n"
    "• <code>.unpin</code> (reply) — unpin the replied message.\n"
    "• <code>.unpin</code> — unpin the last pinned message.\n"
    "• <code>.unpinall</code> — unpin all pinned messages in this chat.\n\n"
    "<i>Only full admins can use these. Bot needs Pin Messages permission.</i>"
)
COMMANDS = [
    ("pin", "Pin a replied message"),
    ("unpin", "Unpin a message"),
    ("unpinall", "Unpin all messages"),
]


async def _is_full_admin(ctx, chat_id: int, user_id: int) -> bool:
    try:
        m = await ctx.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    if m.status == ChatMemberStatus.OWNER:
        return True
    if m.status != ChatMemberStatus.ADMINISTRATOR:
        return False
    return bool(getattr(m, "can_pin_messages", False))


# ───────────── commands ─────────────

async def pin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return
    if not await _is_full_admin(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("⚠️ only full-power admins can pin."), reply_to=msg.message_id)
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
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return
    if not await _is_full_admin(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("⚠️ only full-power admins can unpin."), reply_to=msg.message_id)
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
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return
    if not await _is_full_admin(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("⚠️ only full-power admins can unpin."), reply_to=msg.message_id)
        return
    try:
        await ctx.bot.unpin_all_chat_messages(chat.id)
        await say(ctx, chat.id, T("📌 all messages unpinned."), reply_to=msg.message_id)
    except TelegramError as e:
        await say(ctx, chat.id, T(f"❌ couldn't unpin all: {e}"), reply_to=msg.message_id)


# ───────────── registration ─────────────

def register(app):
    dual_command(app, "pin", pin_cmd)
    dual_command(app, "unpin", unpin_cmd)
    dual_command(app, "unpinall", unpinall_cmd)
