"""Clean Service — granular auto-delete of system/service messages in groups."""
import asyncio
import logging

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import Application, MessageHandler, filters, ContextTypes

import database as dbase
from common import T, dual_command, say

log = logging.getLogger("cleanservice")


# ─────────────────────────── TYPE MAP ───────────────────────────
# Each logical category → telegram Message attributes that identify it.
_TYPE_MAP = {
    "join": (
        "new_chat_members", "group_chat_created",
        "supergroup_chat_created", "channel_chat_created",
    ),
    "leave": ("left_chat_member",),
    "photo": ("new_chat_photo", "delete_chat_photo", "chat_background_set"),
    "pin": ("pinned_message",),
    "title": (
        "new_chat_title", "forum_topic_created", "forum_topic_closed",
        "forum_topic_reopened", "forum_topic_edited",
        "general_forum_topic_hidden", "general_forum_topic_unhidden",
    ),
    "videochat": (
        "video_chat_scheduled", "video_chat_started", "video_chat_ended",
        "video_chat_participants_invited",
    ),
    "other": (
        "migrate_to_chat_id", "migrate_from_chat_id",
        "message_auto_delete_timer_changed", "write_access_allowed",
        "boost_added", "proximity_alert_triggered",
    ),
}

_ALL_TYPES = tuple(_TYPE_MAP.keys())

_TYPE_DESC = {
    "join": "new member joined / chat created",
    "leave": "member left or was removed",
    "photo": "chat photo or background changed",
    "pin": "a message was pinned",
    "title": "chat / topic title changed",
    "videochat": "video chat started / ended / scheduled",
    "other": "boosts, payments, auto-delete, proximity, etc.",
}

HELP_TXT = (
    "<b>🧼 𝘾𝙡𝙚𝙖𝙣 𝙎𝙚𝙧𝙫𝙞𝙘𝙚</b>\n\n"
    "Automatically delete system/service messages from the chat.\n\n"
    "<b>Commands:</b>\n"
    "• <code>/cleanservice on</code> — enable ALL types\n"
    "• <code>/cleanservice off</code> — disable everything\n"
    "• <code>/cleanservice &lt;type&gt;</code> — enable a single type\n"
    "• <code>/keepservice &lt;type&gt;</code> — stop deleting a type\n"
    "• <code>/nocleanservice &lt;type&gt;</code> — same as keepservice\n"
    "• <code>/cleanservicetypes</code> — list all available types\n\n"
    "<i>Only full admins with 'Delete Messages' right can use this. "
    "Bot must also have delete rights.</i>"
)

COMMANDS = [
    ("cleanservice", "Auto-delete system/service messages"),
    ("keepservice", "Stop auto-deleting a service-message type"),
    ("nocleanservice", "Same as /keepservice"),
    ("cleanservicetypes", "List available service-message types"),
]


# ─────────────────────────── STATE ───────────────────────────
# chat_id -> set of enabled type names. Empty set == disabled entirely.
_state: dict = {}
_locks: dict = {}


def _lock(chat_id: int) -> asyncio.Lock:
    lk = _locks.get(chat_id)
    if lk is None:
        lk = _locks[chat_id] = asyncio.Lock()
    return lk


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "t", "on", "yes", "y")
    return bool(v)


def _norm_types(raw) -> set:
    """Normalise DB-stored types (str or list) into a valid set."""
    if raw is None:
        return set()
    if isinstance(raw, str):
        parts = [p.strip().lower() for p in raw.replace(";", ",").split(",")]
    else:
        try:
            parts = [str(p).strip().lower() for p in raw]
        except TypeError:
            return set()
    return {p for p in parts if p in _ALL_TYPES}


# ─────────────────────────── DB ───────────────────────────
async def _load(chat_id: int) -> set:
    """Memory-first read. Returns set of enabled types."""
    if chat_id in _state:
        return _state[chat_id]
    try:
        cfg = await dbase.cleanservice_get(chat_id)
    except Exception as e:
        log.warning("[cleanservice] db read failed for %s: %s (treating OFF)", chat_id, e)
        return set()
    types = set()
    if cfg:
        # New-style record: explicit types list
        if cfg.get("types") is not None:
            types = _norm_types(cfg.get("types"))
        # Old-style record: bool -> means "all"
        elif _truthy(cfg.get("enabled")):
            types = set(_ALL_TYPES)
    _state.setdefault(chat_id, types)
    return _state[chat_id]


async def _db_write(chat_id: int, types: set):
    """Try to persist types, falling back to old bool-only setter."""
    payload = sorted(types)
    enabled = bool(types)
    setter = dbase.cleanservice_set
    # Preferred: extended setter (chat_id, enabled, types=[...])
    try:
        await setter(chat_id, enabled, types=payload)
        return
    except TypeError:
        pass
    # Positional variant
    try:
        await setter(chat_id, enabled, payload)
        return
    except TypeError:
        pass
    # Last resort: legacy bool-only setter
    await setter(chat_id, enabled)


async def _save(chat_id: int, types: set):
    """Memory-first write with rollback on DB failure."""
    async with _lock(chat_id):
        previous = _state.get(chat_id)
        _state[chat_id] = set(types)
        try:
            await _db_write(chat_id, types)
        except Exception as e:
            log.error("[cleanservice] db write failed for %s: %s — rolling back", chat_id, e)
            if previous is None:
                _state.pop(chat_id, None)
            else:
                _state[chat_id] = previous
            raise


# ─────────────────────────── PERMS ───────────────────────────
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


# ─────────────────────────── HELPERS ───────────────────────────
def _classify(msg) -> set:
    """Return logical types that this service message belongs to."""
    kinds = set()
    for t, attrs in _TYPE_MAP.items():
        if any(getattr(msg, a, None) for a in attrs):
            kinds.add(t)
    if not kinds:
        kinds.add("other")   # fallback: any unrecognised service message
    return kinds


def _types_text() -> str:
    lines = ["<b>🧼 Available types:</b>", "• <code>all</code> — every service message"]
    for t in _ALL_TYPES:
        lines.append(f"• <code>{t}</code> — {_TYPE_DESC[t]}")
    return "\n".join(lines)


def _status_text(current: set) -> str:
    if current >= set(_ALL_TYPES):
        state = "🟢 ON (all types)"
    elif current:
        state = "🟢 ON — " + ", ".join(sorted(current))
    else:
        state = "🔴 OFF"
    return f"<blockquote>🧼 Clean Service is <b>{state}</b>.</blockquote>"


# ─────────────────────────── HANDLERS ───────────────────────────
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

    args = [a.lower() for a in (ctx.args or [])]
    current = await _load(chat.id)

    # No args → status
    if not args:
        await say(ctx, chat.id, T(_status_text(current)), reply_to=msg.message_id)
        return

    arg = args[0]

    if arg in ("on", "yes", "all"):
        new_types = set(_ALL_TYPES)
    elif arg in ("off", "no", "none"):
        new_types = set()
    elif arg in _ALL_TYPES:
        new_types = set(current)
        new_types.add(arg)
    else:
        await say(ctx, chat.id, T(
            f"<blockquote>❓ unknown type <code>{arg}</code>.\n\n{_types_text()}</blockquote>"
        ), reply_to=msg.message_id)
        return

    try:
        await _save(chat.id, new_types)
    except Exception:
        await say(ctx, chat.id, T("<blockquote>⚠️ couldn't save setting — try again.</blockquote>"), reply_to=msg.message_id)
        return

    await say(ctx, chat.id, T(_status_text(new_types)), reply_to=msg.message_id)


async def keepservice_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Also used by /nocleanservice."""
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return

    if not await _is_full_admin(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ only full-power admins can use this.</blockquote>"), reply_to=msg.message_id)
        return

    args = [a.lower() for a in (ctx.args or [])]
    current = await _load(chat.id)

    if not args or args[0] in ("all", "on", "yes"):
        new_types = set()
    else:
        arg = args[0]
        if arg not in _ALL_TYPES:
            await say(ctx, chat.id, T(
                f"<blockquote>❓ unknown type <code>{arg}</code>.\n\n{_types_text()}</blockquote>"
            ), reply_to=msg.message_id)
            return
        new_types = set(current)
        new_types.discard(arg)

    try:
        await _save(chat.id, new_types)
    except Exception:
        await say(ctx, chat.id, T("<blockquote>⚠️ couldn't save setting — try again.</blockquote>"), reply_to=msg.message_id)
        return

    await say(ctx, chat.id, T(_status_text(new_types)), reply_to=msg.message_id)


async def cleanservicetypes_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return
    await say(ctx, chat.id, T(f"<blockquote>{_types_text()}</blockquote>"), reply_to=msg.message_id)


async def _service_watcher(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Deletes service messages ONLY for the types enabled in this chat."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    enabled = await _load(chat.id)
    if not enabled:
        return

    kinds = _classify(msg)
    if not (kinds & enabled):
        return

    # Network call → state may have changed meanwhile; re-check after.
    if not await _bot_can_delete(ctx, chat.id):
        return

    enabled = await _load(chat.id)
    if not (kinds & enabled):
        return

    try:
        await ctx.bot.delete_message(chat.id, msg.message_id)
        log.info("[cleanservice] ✅ deleted %s [%s] in chat %s",
                 msg.message_id, ",".join(sorted(kinds)), chat.id)
    except TelegramError as e:
        log.warning("[cleanservice] ❌ delete failed [%s]: %s",
                    ",".join(sorted(kinds)), e)


def register(app: Application):
    dual_command(app, "cleanservice", cleanservice_cmd)
    dual_command(app, "keepservice", keepservice_cmd)
    dual_command(app, "nocleanservice", keepservice_cmd)
    dual_command(app, "cleanservicetypes", cleanservicetypes_cmd)

    # Run after other service handlers but before cleancommand
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.ALL & filters.ChatType.GROUPS,
            _service_watcher,
        ),
        group=97,
    )
