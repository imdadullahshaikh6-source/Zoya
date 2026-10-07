"""Clean Service — granular auto-delete of system/service messages with interactive buttons."""
import asyncio
import logging
import html

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CallbackQueryHandler

import database as dbase
from common import T, dual_command, say

log = logging.getLogger("cleanservice")


# ─────────────────────────── TYPE MAP ───────────────────────────
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

# ✅ FIX: &lt;type&gt; use kiya taaki Telegram HTML parse error na de
HELP_TXT = (
    "🧼 Clean Service\n\n"
    "Automatically delete system/service messages from the chat.\n\n"
    "Commands:\n"
    "• /cleanservice on — enable ALL types\n"
    "• /cleanservice off — disable everything\n"
    "• /cleanservice &lt;type&gt; — enable a single type\n"
    "• /keepservice &lt;type&gt; — stop deleting a type\n"
    "• /nocleanservice &lt;type&gt; — same as keepservice\n"
    "• /cleanservicetypes — list all available types\n\n"
    "Only full admins with 'Delete Messages' right can use this. "
    "Bot must also have delete rights."
)

COMMANDS = [
    ("cleanservice", "Auto-delete system/service messages"),
    ("keepservice", "Stop auto-deleting a service-message type"),
    ("nocleanservice", "Same as /keepservice"),
    ("cleanservicetypes", "Show interactive type buttons"),
]


# ─────────────────────────── STATE ───────────────────────────
_state: dict = {}          # chat_id -> set of enabled type names
_locks: dict = {}          # chat_id -> asyncio.Lock


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
    if chat_id in _state:
        return _state[chat_id]
    try:
        cfg = await dbase.cleanservice_get(chat_id)
    except Exception as e:
        log.warning("[cleanservice] db read failed for %s: %s (treating OFF)", chat_id, e)
        return set()
    types = set()
    if cfg:
        if cfg.get("types") is not None:
            types = _norm_types(cfg.get("types"))
        elif _truthy(cfg.get("enabled")):
            types = set(_ALL_TYPES)
    _state.setdefault(chat_id, types)
    return _state[chat_id]


async def _db_write(chat_id: int, types: set):
    payload = sorted(types)
    enabled = bool(types)
    setter = dbase.cleanservice_set
    try:
        await setter(chat_id, enabled, types=payload)
        return
    except TypeError:
        pass
    try:
        await setter(chat_id, enabled, payload)
        return
    except TypeError:
        pass
    await setter(chat_id, enabled)


async def _save(chat_id: int, types: set):
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
    kinds = set()
    for t, attrs in _TYPE_MAP.items():
        if any(getattr(msg, a, None) for a in attrs):
            kinds.add(t)
    if not kinds:
        kinds.add("other")
    return kinds


def _types_text() -> str:
    lines = ["🧼 Available types:", "• all — every service message"]
    for t in _ALL_TYPES:
        lines.append(f"• {t} — {_TYPE_DESC[t]}")
    return "\n".join(lines)


def _status_text(current: set) -> str:
    if current >= set(_ALL_TYPES):
        state = "🟢 ON (all types)"
    elif current:
        state = "🟢 ON — " + ", ".join(sorted(current))
    else:
        state = "🔴 OFF"
    return f"🧼 Clean Service is {state}."


# ─────────────────────────── BUTTONS ───────────────────────────
def _types_keyboard(current: set) -> InlineKeyboardMarkup:
    buttons = []
    row = []
    
    all_on = current >= set(_ALL_TYPES)
    all_text = "🟢 ALL" if all_on else "🔴 ALL"
    buttons.append([InlineKeyboardButton(all_text, callback_data="cs_toggle:all")])

    for t in _ALL_TYPES:
        status = "🟢" if t in current else "🔴"
        label = "video" if t == "videochat" else t
        row.append(InlineKeyboardButton(
            f"{status} {label.capitalize()}",
            callback_data=f"cs_toggle:{t}"
        ))
        if len(row) == 3:
            buttons.append(row)
            row = []
    
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton("❌ Close", callback_data="cs_close")])
    return InlineKeyboardMarkup(buttons)


# ─────────────────────────── HANDLERS ───────────────────────────
async def cleanservice_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return

    if not await _is_full_admin(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("⚠️ only full-power admins can use this."), reply_to=msg.message_id)
        return

    if not await _bot_can_delete(ctx, chat.id):
        await say(ctx, chat.id, T("⚠️ make me an admin with Delete Messages permission first."), reply_to=msg.message_id)
        return

    args = [a.lower() for a in (ctx.args or [])]
    current = await _load(chat.id)

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
        # ✅ FIX: escape user input to prevent HTML parsing errors
        safe_arg = html.escape(arg)
        await say(ctx, chat.id, T(f"❓ unknown type {safe_arg}.\n\n{_types_text()}"), reply_to=msg.message_id)
        return

    try:
        await _save(chat.id, new_types)
    except Exception:
        await say(ctx, chat.id, T("⚠️ couldn't save setting — try again."), reply_to=msg.message_id)
        return

    await say(ctx, chat.id, T(_status_text(new_types)), reply_to=msg.message_id)


async def keepservice_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return

    if not await _is_full_admin(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("⚠️ only full-power admins can use this."), reply_to=msg.message_id)
        return

    args = [a.lower() for a in (ctx.args or [])]
    current = await _load(chat.id)

    if not args or args[0] in ("all", "on", "yes"):
        new_types = set()
    else:
        arg = args[0]
        if arg not in _ALL_TYPES:
            safe_arg = html.escape(arg)
            await say(ctx, chat.id, T(f"❓ unknown type {safe_arg}.\n\n{_types_text()}"), reply_to=msg.message_id)
            return
        new_types = set(current)
        new_types.discard(arg)

    try:
        await _save(chat.id, new_types)
    except Exception:
        await say(ctx, chat.id, T("⚠️ couldn't save setting — try again."), reply_to=msg.message_id)
        return

    await say(ctx, chat.id, T(_status_text(new_types)), reply_to=msg.message_id)


async def cleanservicetypes_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return

    current = await _load(chat.id)
    
    await ctx.bot.send_message(
        chat.id,
        T("Tap a button to toggle that service message type:"),
        reply_markup=_types_keyboard(current),
        reply_to_message_id=msg.message_id,
    )


async def _cs_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    chat = update.effective_chat
    user = update.effective_user

    if data == "cs_close":
        try:
            await query.message.delete()
        except TelegramError:
            pass
        return

    if not data.startswith("cs_toggle:"):
        return

    if not await _is_full_admin(ctx, chat.id, user.id):
        await query.answer("⚠️ Only full admins can use this!", show_alert=True)
        return

    if not await _bot_can_delete(ctx, chat.id):
        await query.answer("⚠️ I need Delete Messages permission!", show_alert=True)
        return

    arg = data.split(":")[1]
    current = await _load(chat.id)

    if arg == "all":
        new_types = set() if current >= set(_ALL_TYPES) else set(_ALL_TYPES)
    elif arg in _ALL_TYPES:
        new_types = set(current)
        if arg in new_types:
            new_types.discard(arg)
        else:
            new_types.add(arg)
    else:
        return

    try:
        await _save(chat.id, new_types)
    except Exception:
        await query.answer("⚠️ DB error, try again.", show_alert=True)
        return

    try:
        await query.edit_message_reply_markup(reply_markup=_types_keyboard(new_types))
    except TelegramError:
        pass


async def _service_watcher(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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

    app.add_handler(CallbackQueryHandler(_cs_callback, pattern=r"^cs_"))

    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.ALL & filters.ChatType.GROUPS,
            _service_watcher,
        ),
        group=97,
    )
