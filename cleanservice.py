"""Clean Service — auto-delete system/service messages in groups."""
import asyncio
import logging

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import Application, MessageHandler, filters, ContextTypes

import database as dbase
from common import T, dual_command, say

log = logging.getLogger("cleanservice")

HELP_TXT = (
    "<b>🧼 𝘾𝙡𝙚𝙖𝙣 𝙎𝙚𝙧𝙫𝙞𝙘𝙚</b>\n\n"
    "Automatically delete system/service messages from the chat.\n"
    "This includes: joins, leaves, pins, video chat started/ended, "
    "new group photo, new title, etc.\n\n"
    "<b>Commands:</b>\n"
    "• <code>/cleanservice on</code> (or <code>.cleanservice on</code>) — enable auto-deletion of service messages.\n"
    "• <code>/cleanservice off</code> (or <code>.cleanservice off</code>) — disable.\n"
    "• <code>/keepservice</code> (or <code>.keepservice</code>) — same as off (keeps service messages).\n\n"
    "<i>Only full admins with 'Delete Messages' right can use this. "
    "Bot must also have delete rights.</i>"
)

COMMANDS = [
    ("cleanservice", "Auto-delete system/service messages"),
    ("keepservice", "Stop auto-deleting service messages"),
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
    return bool(getattr(m, "can_delete_messages", False))


async def _bot_can_delete(ctx, chat_id: int) -> bool:
    me = ctx.application.bot_data.get("me")
    if not me:
        return False
    try:
        m = await ctx.bot.get_chat_member(chat_id, me.id)
    except TelegramError:
        return False
    return (m.status == ChatMemberStatus.ADMINISTRATOR
            and bool(getattr(m, "can_delete_messages", False)))


async def cleanservice_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return

    if not await _is_full_admin(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ only full-power admins can use this.</blockquote>"), reply_to=msg.message_id)
        return

    if not await _bot_can_delete(ctx, chat.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ make me an admin with <b>Delete Messages</b> permission first.</blockquote>"), reply_to=msg.message_id)
        return

    arg = (ctx.args[0].lower() if ctx.args else "")
    cfg = await dbase.cleanservice_get(chat.id) or {}
    current = cfg.get("enabled", False)

    if arg not in ("on", "off", "yes", "no"):
        state = "🟢 ON" if current else "🔴 OFF"
        await say(ctx, chat.id, T(
            f"<blockquote>🧼 Cleanservice is currently <b>{state}</b>.\n"
            f"Usage: <code>/cleanservice on</code> / <code>/cleanservice off</code></blockquote>"
        ), reply_to=msg.message_id)
        return

    enabled = arg in ("on", "yes")
    await dbase.cleanservice_set(chat.id, enabled)
    await say(ctx, chat.id, T(
        f"<blockquote>🧼 Cleanservice turned <b>{'ON ✅' if enabled else 'OFF ❌'}</b>.</blockquote>"
    ), reply_to=msg.message_id)


async def keepservice_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return

    if not await _is_full_admin(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ only full-power admins can use this.</blockquote>"), reply_to=msg.message_id)
        return

    await dbase.cleanservice_set(chat.id, False)
    await say(ctx, chat.id, T("<blockquote>🧼 Cleanservice turned <b>OFF ❌</b> — service messages will stay.</blockquote>"), reply_to=msg.message_id)


async def _service_watcher(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Deletes service messages when enabled."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    cfg = await dbase.cleanservice_get(chat.id)
    if not cfg or not cfg.get("enabled"):
        return

    # Bot must have delete rights
    if not await _bot_can_delete(ctx, chat.id):
        return

    try:
        await ctx.bot.delete_message(chat.id, msg.message_id)
        log.info("[cleanservice] ✅ deleted service message %s in chat %s", msg.message_id, chat.id)
    except TelegramError as e:
        log.warning("[cleanservice] ❌ delete failed: %s", e)


def register(app: Application):
    dual_command(app, "cleanservice", cleanservice_cmd)
    dual_command(app, "keepservice", keepservice_cmd)

    # Run after other service handlers but before cleancommand
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.ALL & filters.ChatType.GROUPS,
            _service_watcher,
        ),
        group=97,
  )
