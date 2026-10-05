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


# ── in-memory source of truth (instantly honours /keepservice, no stale DB reads) ──
_state: dict = {}                 # chat_id -> bool
_locks: dict = {}                 # chat_id -> asyncio.Lock

# service-message types, only used for logging which one got deleted
_SERVICE_ATTRS = (
    "new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo",
    "delete_chat_photo", "group_chat_created", "supergroup_chat_created",
    "channel_chat_created", "message_auto_delete_timer_changed", "migrate_to_chat_id",
    "migrate_from_chat_id", "pinned_message", "video_chat_scheduled",
    "video_chat_started", "video_chat_ended", "video_chat_participants_invited",
    "forum_topic_created", "forum_topic_closed", "forum_topic_reopened",
    "forum_topic_edited", "general_forum_topic_hidden", "general_forum_topic_unhidden",
    "write_access_allowed", "boost_added", "chat_background_set",
)


def _lock(chat_id: int) -> asyncio.Lock:
    lk = _locks.get(chat_id)
    if lk is None:
        lk = _locks[chat_id] = asyncio.Lock()
    return lk


def _truthy(v) -> bool:
    """DB may return True/1/'1'/'true'/'False'... normalise safely."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "t", "on", "yes", "y")
    return bool(v)


async def _is_enabled(chat_id: int) -> bool:
    if chat_id in _state:
        return _state[chat_id]
    try:
        cfg = await dbase.cleanservice_get(chat_id)
    except Exception as e:
        log.warning("[cleanservice] db read failed for %s: %s (treating as OFF)", chat_id, e)
        return False                      # fail-safe: never delete if unsure
    val = _truthy(cfg.get("enabled")) if cfg else False
    _state.setdefault(chat_id, val)       # don't overwrite a value set while we were awaiting
    return _state[chat_id]


async def _set_enabled(chat_id: int, value: bool):
    """Memory-first write with rollback on DB failure."""
    async with _lock(chat_id):
        previous = _state.get(chat_id)
        _state[chat_id] = value           # optimistic: cache first so watcher obeys instantly
        try:
            await dbase.cleanservice_set(chat_id, value)
        except Exception as e:
            log.error("[cleanservice] db write failed for %s: %s — rolling back cache", chat_id, e)
            if previous is None:
                _state.pop(chat_id, None)
            else:
                _state[chat_id] = previous
            raise   # re-raise so the command can report failure


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
    try:
        m = await ctx.bot.get_chat_member(chat_id, ctx.bot.id)
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
    current = await _is_enabled(chat.id)

    if arg not in ("on", "off", "yes", "no"):
        state = "🟢 ON" if current else "🔴 OFF"
        await say(ctx, chat.id, T(
            f"<blockquote>🧼 Cleanservice is currently <b>{state}</b>.\n"
            f"Usage: <code>/cleanservice on</code> / <code>/cleanservice off</code></blockquote>"
        ), reply_to=msg.message_id)
        return

    enabled = arg in ("on", "yes")

    try:
        await _set_enabled(chat.id, enabled)
    except Exception:
        await say(ctx, chat.id, T("<blockquote>⚠️ couldn't save setting — try again.</blockquote>"), reply_to=msg.message_id)
        return

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

    try:
        await _set_enabled(chat.id, False)
    except Exception:
        await say(ctx, chat.id, T("<blockquote>⚠️ couldn't save setting — try again.</blockquote>"), reply_to=msg.message_id)
        return

    await say(ctx, chat.id, T("<blockquote>🧼 Cleanservice turned <b>OFF ❌</b> — service messages will stay.</blockquote>"), reply_to=msg.message_id)


async def _service_watcher(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Deletes service messages ONLY while cleanservice is ON."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not await _is_enabled(chat.id):
        return

    # Bot must have delete rights (network call -> state may change meanwhile)
    if not await _bot_can_delete(ctx, chat.id):
        return

    # re-check right before deleting: /keepservice may have run during the await above
    if not await _is_enabled(chat.id):
        return

    kinds = [a for a in _SERVICE_ATTRS if getattr(msg, a, None)] or ["unknown"]
    try:
        await ctx.bot.delete_message(chat.id, msg.message_id)
        log.info("[cleanservice] ✅ deleted %s (%s) in chat %s", msg.message_id, ",".join(kinds), chat.id)
    except TelegramError as e:
        log.warning("[cleanservice] ❌ delete failed (%s): %s", ",".join(kinds), e)


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
