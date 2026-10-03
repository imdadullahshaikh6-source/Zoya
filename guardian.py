"""Guardian — anti-edit + anti-media defender with delayed deletion.

Only works when the bot is a full admin (can_delete_messages) in the group.
Only admins with delete permissions (or the group owner) can configure it.

Commands:
  .setdelay <1m..6h>   — set deletion delay for this chat
  .guard on|off        — enable/disable guardian in this chat
  .permit   (reply)    — whitelist a user (owner only)
  .unpermit (reply)    — remove a user from the whitelist (owner only)
  .permitlist          — show whitelisted users (owner only)

NOTE: Once enabled, ALL users' edited messages and media get deleted after the
delay — including admins and the group owner. Only the bot's own messages and
permitted users are exempt.

All data is stored in MongoDB per chat_id, so it survives bot restarts.
"""
import asyncio
import html
import logging
import re

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes, MessageHandler, filters

import database as dbase
from common import T, dual_command, mention, say

log = logging.getLogger("guardian")

HELP_TXT = (
    "<b>🛡 𝙂𝙪𝙖𝙧𝙙𝙞𝙖𝙣 — Media & Edit Defender</b>\n\n"
    "<b>Features Overview:</b>\n"
    "• Advanced Delayed Edited Message Deletion.\n"
    "• Delayed Media (Photo, Video, Voice, Audio, Doc) Deletion.\n"
    "• Configurable Deletion Timer per chat.\n"
    "• Permit System for trusted users.\n\n"
    "<b>Timer Commands:</b>\n"
    "• <code>.setdelay 5m</code> — set deletion delay to 5 minutes.\n"
    "• <code>.setdelay 6h</code> — set deletion delay to 6 hours.\n"
    "<i>(setdelay range → 1 minute to 6 hours)</i>\n\n"
    "<b>Permit Commands (owner only):</b>\n"
    "• <code>.permit</code> (reply) — whitelist a user (their edits/media won't be deleted).\n"
    "• <code>.unpermit</code> (reply) — remove a user from the permit list.\n"
    "• <code>.permitlist</code> — view permitted users.\n"
    "• <code>.guard on</code> / <code>.guard off</code> — enable/disable Guardian."
)
COMMANDS = [
    ("setdelay", "Set Guardian deletion delay"),
    ("guard", "Enable or disable Guardian"),
    ("permit", "Whitelist a user from Guardian"),
    ("unpermit", "Remove a user from the permit list"),
    ("permitlist", "Show permitted users"),
]

MIN_DELAY = 60           # 1 minute
MAX_DELAY = 6 * 60 * 60  # 6 hours
_DELAY_RE = re.compile(r"^(\d+)\s*([smh])$", re.IGNORECASE)
_NOTE_LIFETIME = 5       # seconds — the notice itself auto-deletes after this


def _parse_delay(arg: str):
    m = _DELAY_RE.match((arg or "").strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    mult = {"s": 1, "m": 60, "h": 3600}[unit]
    return n * mult


def _safe_mention(user) -> str:
    """Safely create an HTML mention, escaping the name to prevent parse errors."""
    if not user:
        return "Unknown"
    name = html.escape(user.first_name or "User")
    return f"<a href='tg://user?id={user.id}'>{name}</a>"


async def _is_owner(ctx, chat_id: int, user_id: int) -> bool:
    """True only if the user is the group owner (creator)."""
    try:
        m = await ctx.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    return m.status == ChatMemberStatus.OWNER


async def _is_full_admin(ctx, chat_id: int, user_id: int) -> bool:
    """True if the user is the owner OR an admin who can delete messages."""
    try:
        m = await ctx.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    if m.status == ChatMemberStatus.OWNER:
        return True
    if m.status != ChatMemberStatus.ADMINISTRATOR:
        return False
    return bool(getattr(m, "can_delete_messages", False))


async def _bot_can_guard(ctx, chat_id: int) -> bool:
    me = ctx.application.bot_data.get("me")
    if not me:
        return False
    try:
        m = await ctx.bot.get_chat_member(chat_id, me.id)
    except TelegramError:
        return False
    return (m.status == ChatMemberStatus.ADMINISTRATOR
            and bool(getattr(m, "can_delete_messages", False)))


# ───────────── commands ─────────────

async def setdelay_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return
    if not await _is_full_admin(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ only full-power admins can use this.</blockquote>"), reply_to=msg.message_id)
        return
    if not await _bot_can_guard(ctx, chat.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ make me an admin with <b>Delete Messages</b> permission first.</blockquote>"), reply_to=msg.message_id)
        return
    if not ctx.args:
        await say(ctx, chat.id, T("<blockquote>usage: <code>.setdelay 5m</code> — range 1m to 6h</blockquote>"), reply_to=msg.message_id)
        return
    delay = _parse_delay(ctx.args[0])
    if delay is None or delay < MIN_DELAY or delay > MAX_DELAY:
        await say(ctx, chat.id, T("<blockquote>❌ invalid delay — use <code>1m</code> to <code>6h</code> (e.g. <code>.setdelay 10m</code>)</blockquote>"), reply_to=msg.message_id)
        return
    await dbase.guardian_set(chat.id, delay_seconds=delay, enabled=True)
    mins = delay // 60
    await say(ctx, chat.id, T(
        f"<blockquote>✅ Guardian active — deletion delay set to <b>{mins} min</b>.\n"
        f"<i>everyone's edits & media will be deleted — including admins.</i></blockquote>"
    ), reply_to=msg.message_id)


async def guard_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return
    if not await _is_full_admin(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ only full-power admins can use this.</blockquote>"), reply_to=msg.message_id)
        return
    arg = (ctx.args[0].lower() if ctx.args else "")
    cfg = await dbase.guardian_get(chat.id) or {}
    if arg not in ("on", "off"):
        state = "🟢 ON" if cfg.get("enabled") else "🔴 OFF"
        delay = cfg.get("delay_seconds", 0)
        await say(ctx, chat.id, T(
            f"<blockquote>Guardian is <b>{state}</b> — delay <b>{delay // 60} min</b>.\n"
            f"usage: <code>.guard on</code> / <code>.guard off</code></blockquote>"
        ), reply_to=msg.message_id)
        return
    enabled = arg == "on"
    await dbase.guardian_set(chat.id, enabled=enabled)
    await say(ctx, chat.id, T(f"<blockquote>✅ Guardian turned <b>{'ON' if enabled else 'OFF'}</b>.</blockquote>"), reply_to=msg.message_id)


async def permit_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return
    if not await _is_owner(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ only the <b>group owner</b> can use this.</blockquote>"), reply_to=msg.message_id)
        return
    target = msg.reply_to_message.from_user if msg.reply_to_message else None
    if not target:
        await say(ctx, chat.id, T("<blockquote>reply to a user's message with <code>.permit</code>.</blockquote>"), reply_to=msg.message_id)
        return
    safe_name = html.escape(target.first_name or "User")
    await dbase.guardian_permit_add(chat.id, target.id, safe_name)
    await say(ctx, chat.id, T(f"<blockquote>✅ {_safe_mention(target)} is now <b>permitted</b> — their edits/media won't be deleted.</blockquote>"), reply_to=msg.message_id)


async def unpermit_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return
    if not await _is_owner(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ only the <b>group owner</b> can use this.</blockquote>"), reply_to=msg.message_id)
        return
    target = msg.reply_to_message.from_user if msg.reply_to_message else None
    if not target:
        await say(ctx, chat.id, T("<blockquote>reply to a user's message with <code>.unpermit</code>.</blockquote>"), reply_to=msg.message_id)
        return
    removed = await dbase.guardian_permit_remove(chat.id, target.id)
    if removed:
        await say(ctx, chat.id, T(f"<blockquote>✅ {_safe_mention(target)} is no longer permitted.</blockquote>"), reply_to=msg.message_id)
    else:
        await say(ctx, chat.id, T(f"<blockquote>ℹ️ {_safe_mention(target)} wasn't in the permit list.</blockquote>"), reply_to=msg.message_id)


async def permitlist_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return
    if not await _is_owner(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ only the <b>group owner</b> can use this.</blockquote>"), reply_to=msg.message_id)
        return
    cfg = await dbase.guardian_get(chat.id) or {}
    users = cfg.get("permitted_users", [])
    if not users:
        await say(ctx, chat.id, T("<blockquote>no permitted users in this chat.</blockquote>"), reply_to=msg.message_id)
        return
    lines = ["<b>🛡 Permitted users:</b>"]
    for i, u in enumerate(users, 1):
        # FIX: Escape the name to prevent "Can't parse entities" error
        safe_name = html.escape(str(u.get('name', 'Unknown')))
        lines.append(f"{i}. {safe_name} — <code>{u['id']}</code>")
    text_body = "\n".join(lines)
    await say(ctx, chat.id, T(f"<blockquote>{text_body}</blockquote>"), reply_to=msg.message_id)


# ───────────── watcher ─────────────

def _msg_has_media(msg) -> bool:
    return bool(
        msg.photo or msg.video or msg.video_note
        or msg.voice or msg.audio or msg.document
        or msg.animation or msg.sticker
    )


async def _delete_after(ctx, chat_id: int, message_id: int, delay: int, note_text: str):
    """Wait `delay` seconds, delete the original message, post a quote-block
    notice, then delete that notice after `_NOTE_LIFETIME` seconds."""
    await asyncio.sleep(delay)
    try:
        await ctx.bot.delete_message(chat_id, message_id)
        log.info("[guardian] deleted msg %s in chat %s", message_id, chat_id)
    except TelegramError as e:
        log.warning("[guardian] delete failed for msg %s: %s", message_id, e)
        return
    try:
        note = await ctx.bot.send_message(chat_id, note_text, parse_mode=ParseMode.HTML)
        await asyncio.sleep(_NOTE_LIFETIME)
        try:
            await ctx.bot.delete_message(chat_id, note.message_id)
        except TelegramError:
            pass
    except TelegramError as e:
        log.warning("[guardian] note failed: %s", e)


async def _should_guard(ctx, chat, user) -> int:
    """Return the delay in seconds if guardian should act, else 0."""
    if not chat or chat.type == ChatType.PRIVATE:
        return 0
    me = ctx.application.bot_data.get("me")
    if me and user and user.id == me.id:
        return 0
    if not user or user.is_bot:
        return 0
    cfg = await dbase.guardian_get(chat.id)
    if not cfg or not cfg.get("enabled"):
        log.debug("[guardian] skip: not enabled for chat %s", chat.id)
        return 0
    delay = int(cfg.get("delay_seconds") or 0)
    if delay < MIN_DELAY:
        log.debug("[guardian] skip: delay too small (%s)", delay)
        return 0
    permitted = {u.get("id") for u in cfg.get("permitted_users", [])}
    if user.id in permitted:
        log.debug("[guardian] skip: user %s permitted", user.id)
        return 0
    if not await _bot_can_guard(ctx, chat.id):
        log.debug("[guardian] skip: bot lacks delete permission in %s", chat.id)
        return 0
    return delay


async def _guardian_watcher(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Single robust watcher — fires on EVERY group message and decides
    internally whether it's an edit or new media that needs guarding."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    if chat.type == ChatType.PRIVATE:
        return
    if user.is_bot:
        return

    is_edit = bool(msg.edit_date)
    is_media = _msg_has_media(msg)

    if not is_edit and not is_media:
        return

    log.info(
        "[guardian] caught update: chat=%s user=%s edit=%s media=%s msg_id=%s",
        chat.id, user.id, is_edit, is_media, msg.message_id,
    )

    delay = await _should_guard(ctx, chat, user)
    log.info("[guardian] delay_seconds=%s", delay)
    if not delay:
        return

    # ✅ Quote block formatting with Safe Mention
    if is_edit:
        body = f"🗑️ {_safe_mention(user)}'s <b>edited message</b> was deleted."
    else:
        body = f"🗑️ {_safe_mention(user)}'s <b>media</b> was deleted."
    note = f"<blockquote>{body}</blockquote>"

    log.info("[guardian] scheduling deletion of msg %s in %ss", msg.message_id, delay)
    asyncio.create_task(_delete_after(ctx, chat.id, msg.message_id, delay, note))


# ───────────── registration ─────────────

def register(app):
    dual_command(app, "setdelay", setdelay_cmd)
    dual_command(app, "guard", guard_cmd)
    dual_command(app, "permit", permit_cmd)
    dual_command(app, "unpermit", unpermit_cmd)
    dual_command(app, "permitlist", permitlist_cmd)

    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.ALL, _guardian_watcher),
        group=2,
                 )
