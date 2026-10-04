"""Locks plugin: lock types to auto-delete specific message types (MongoDB)."""
import html
import re
import time

from telegram import Update, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes, MessageHandler, filters, CallbackQueryHandler

import database as dbase
from common import B, T, dual_command, say, resolve_target

BOT_OWNER_ID = 8373739674
ANON_ADMIN_ID = 1087968824

COMMANDS = [
    ("lock", "Lock message types"),
    ("unlock", "Unlock message types"),
    ("locks", "List currently locked types"),
    ("locktypes", "Show all lockable types"),
    ("approve", "Approve a user (locks won't apply)"),
    ("unapprove", "Unapprove a user"),
    ("approved", "List approved users"),
]

LOCKTYPES = [
    "all", "album", "anonchannel", "audio", "bot", "botlink", "button",
    "contact", "document", "email", "emoji", "emojicustom", "forward",
    "gif", "invitelink", "url", "location", "phone", "photo", "poll",
    "spoiler", "sticker", "text", "video", "videonote", "voice"
]

LOCK_DESC = {
    "all": "Lock all message types.",
    "album": "Messages containing multiple photos/videos.",
    "anonchannel": "Messages sent anonymously by channels.",
    "audio": "Audio files.",
    "bot": "Messages sent by bots.",
    "botlink": "Links to Telegram bots.",
    "button": "Messages with inline buttons.",
    "contact": "Shared contacts.",
    "document": "Documents/files.",
    "email": "Email addresses.",
    "emoji": "Emojis.",
    "emojicustom": "Premium custom emojis.",
    "forward": "Forwarded messages.",
    "gif": "GIFs/Animations.",
    "invitelink": "Messages containing private and public links (or usernames) to telegram groups or channels. Can be allowlisted.",
    "url": "URLs/Links (except group's own links).",
    "location": "Locations.",
    "phone": "Phone numbers.",
    "photo": "Photos.",
    "poll": "Polls.",
    "spoiler": "Spoilers.",
    "sticker": "Stickers.",
    "text": "Plain text messages (no media).",
    "video": "Videos.",
    "videonote": "Video notes (round videos).",
    "voice": "Voice messages."
}

ANON_PENDING = {}

_ADMIN_CACHE = {}
_CACHE_TTL = 600

_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA70-\U0001FAFF"
    "]+",
    flags=re.UNICODE,
)


def _is_admin_cached(chat_id: int, user_id: int):
    entry = _ADMIN_CACHE.get((chat_id, user_id))
    if entry:
        status, ts = entry
        if time.time() - ts < _CACHE_TTL:
            return status
    return None


def _cache_admin(chat_id: int, user_id: int, is_admin: bool):
    _ADMIN_CACHE[(chat_id, user_id)] = (is_admin, time.time())


def get_locktypes_kb(back: bool = False):
    buttons = []
    row = []
    for lt in LOCKTYPES:
        display_name = "".join(
            [chr(ord(c) - 97 + 0x1D68A) if 'a' <= c <= 'z' else c for c in lt]
        )
        row.append(B(display_name, f"lockinfo:{lt}", style="primary"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    if back:
        buttons.append([B("⬅ 𝘽𝙖𝙘𝙠", "help:locks", style="danger")])
    return InlineKeyboardMarkup(buttons)


def _has_media(msg) -> bool:
    return bool(msg.photo or msg.video or msg.audio or msg.voice or
                msg.document or msg.sticker or msg.animation or msg.video_note)


async def _get_real_group_owner_id(ctx, chat_id: int):
    try:
        admins = await ctx.bot.get_chat_administrators(chat_id)
        for admin in admins:
            if admin.status == ChatMemberStatus.OWNER:
                return admin.user.id
    except TelegramError:
        pass
    return None


async def _is_full_admin_for_locks(ctx, chat_id: int, user_id: int) -> bool:
    if user_id == BOT_OWNER_ID:
        return True
    try:
        m = await ctx.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    if m.status == ChatMemberStatus.OWNER:
        return True
    if m.status != ChatMemberStatus.ADMINISTRATOR:
        return False
    return bool(getattr(m, "can_change_info", False))


async def _apply_lock(chat_id: int, items: list):
    data = await dbase.locks_get(chat_id)
    locks = data["locks"]
    unlocks = data["unlocks"]
    if "all" in items:
        locks = {"all"}
        unlocks = set()
    else:
        for item in items:
            locks.add(item)
            unlocks.discard(item)
    await dbase.locks_set_locks(chat_id, locks)
    await dbase.locks_set_unlocks(chat_id, unlocks)


async def _apply_unlock(chat_id: int, items: list):
    data = await dbase.locks_get(chat_id)
    locks = data["locks"]
    unlocks = data["unlocks"]
    if "all" in items:
        locks = set()
        unlocks = set()
    else:
        for item in items:
            locks.discard(item)
            unlocks.discard(item)
            if "all" in locks:
                unlocks.add(item)
    if "all" not in locks:
        unlocks = set()
    await dbase.locks_set_locks(chat_id, locks)
    await dbase.locks_set_unlocks(chat_id, unlocks)


def _get_msg_types(msg) -> set:
    types = set()
    has_media = _has_media(msg)

    if msg.text and not has_media:
        types.add("text")

    if msg.photo: types.add("photo")
    if msg.video: types.add("video")
    if msg.audio: types.add("audio")
    if msg.voice: types.add("voice")
    if msg.document: types.add("document")
    if msg.video_note: types.add("videonote")
    if msg.animation: types.add("gif")
    if msg.poll: types.add("poll")
    if msg.contact: types.add("contact")
    if msg.location: types.add("location")
    if msg.media_group_id: types.add("album")
    if msg.reply_markup: types.add("button")

    if msg.sticker:
        types.add("sticker")

    sc = getattr(msg, "sender_chat", None)
    if sc and not getattr(sc, "is_forum", False):
        types.add("anonchannel")
    if msg.from_user and msg.from_user.is_bot:
        types.add("bot")

    # ✅ FIX: getattr use kiya (naye PTB version mein forward_date nahi hai)
    if getattr(msg, "forward_date", None) or getattr(msg, "forward_origin", None):
        types.add("forward")

    if msg.text and _EMOJI_RE.search(msg.text):
        types.add("emoji")

    all_entities = list(msg.entities or []) + list(msg.caption_entities or [])
    for e in all_entities:
        if e.type in ("url", "text_link"):
            src_text = ""
            if msg.text:
                src_text = msg.text[e.offset:e.offset + e.length]
            elif msg.caption:
                src_text = msg.caption[e.offset:e.offset + e.length]
            if "t.me/joinchat" in src_text or "t.me/+" in src_text:
                types.add("invitelink")
            elif "t.me/" in src_text and "bot" in src_text:
                types.add("botlink")
            else:
                types.add("url")
        elif e.type == "email":
            types.add("email")
        elif e.type == "phone_number":
            types.add("phone")
        elif e.type == "spoiler":
            types.add("spoiler")
        elif e.type == "custom_emoji":
            types.add("emoji")
            types.add("emojicustom")

    return types


async def is_message_locked(ctx, chat_id: int, msg) -> bool:
    data = await dbase.locks_get(chat_id)
    active = data.get("locks", set())
    unlocked = data.get("unlocks", set())
    if not active:
        return False

    msg_types = _get_msg_types(msg)

    if "all" in active:
        if not msg_types:
            return True
        for t in msg_types:
            if t in unlocked:
                continue
            return True
        return False

    for t in msg_types:
        if t in active and t not in unlocked:
            return True
    return False


async def lock_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return

    if user.id == ANON_ADMIN_ID:
        if not ctx.args:
            await say(ctx, chat.id, T("usage: /lock <item(s)>"), reply_to=msg.message_id)
            return
        items = [i.lower() for i in ctx.args]
        invalid = [i for i in items if i not in LOCKTYPES]
        if invalid:
            await say(ctx, chat.id, T(f"invalid lock types: {', '.join(invalid)}"), reply_to=msg.message_id)
            return
        token = f"{chat.id}_{msg.message_id}"
        ANON_PENDING[token] = {"action": "lock", "chat_id": chat.id, "items": items}
        kb = InlineKeyboardMarkup([[B("𝙥𝙧𝙤𝙫𝙚 𝙊𝙬𝙣𝙚𝙧/𝙖𝙙𝙢𝙞𝙣", f"anonlock:{token}", style="success")]])
        await ctx.bot.send_message(
            chat.id,
            T("<b>⚠️ Anonymous Admin detected.</b>\nOnly the real group owner can approve locks. Please tap the button below to verify."),
            reply_to_message_id=msg.message_id,
            reply_markup=kb,
        )
        return

    if not await _is_full_admin_for_locks(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("⚠️ only admins with <b>Change Group Info</b> permission can use this."), reply_to=msg.message_id)
        return

    if not ctx.args:
        await say(ctx, chat.id, T("usage: /lock <item(s)>"), reply_to=msg.message_id)
        return

    items = [i.lower() for i in ctx.args]
    invalid = [i for i in items if i not in LOCKTYPES]
    if invalid:
        await say(ctx, chat.id, T(f"invalid lock types: {', '.join(invalid)}"), reply_to=msg.message_id)
        return

    await _apply_lock(chat.id, items)
    await say(ctx, chat.id, T(f"✅ locked: {', '.join(items)}"), reply_to=msg.message_id)


async def unlock_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return

    if user.id == ANON_ADMIN_ID:
        if not ctx.args:
            await say(ctx, chat.id, T("usage: /unlock <item(s)>"), reply_to=msg.message_id)
            return
        items = [i.lower() for i in ctx.args]
        invalid = [i for i in items if i not in LOCKTYPES]
        if invalid:
            await say(ctx, chat.id, T(f"invalid lock types: {', '.join(invalid)}"), reply_to=msg.message_id)
            return
        token = f"{chat.id}_{msg.message_id}"
        ANON_PENDING[token] = {"action": "unlock", "chat_id": chat.id, "items": items}
        kb = InlineKeyboardMarkup([[B("𝙥𝙧𝙤𝙫𝙚 𝙊𝙬𝙣𝙚𝙧/𝙖𝙙𝙢𝙞𝙣", f"anonlock:{token}", style="success")]])
        await ctx.bot.send_message(
            chat.id,
            T("<b>⚠️ Anonymous Admin detected.</b>\nOnly the real group owner can approve unlocks. Please tap the button below to verify."),
            reply_to_message_id=msg.message_id,
            reply_markup=kb,
        )
        return

    if not await _is_full_admin_for_locks(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("⚠️ only admins with <b>Change Group Info</b> permission can use this."), reply_to=msg.message_id)
        return

    if not ctx.args:
        await say(ctx, chat.id, T("usage: /unlock <item(s)>"), reply_to=msg.message_id)
        return

    items = [i.lower() for i in ctx.args]
    invalid = [i for i in items if i not in LOCKTYPES]
    if invalid:
        await say(ctx, chat.id, T(f"invalid lock types: {', '.join(invalid)}"), reply_to=msg.message_id)
        return

    await _apply_unlock(chat.id, items)
    await say(ctx, chat.id, T(f"✅ unlocked: {', '.join(items)}"), reply_to=msg.message_id)


async def approve_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return

    if user.id == ANON_ADMIN_ID:
        target, _ = await resolve_target(update, ctx)
        if not target:
            await say(ctx, chat.id, T("❌ Invalid format. Reply, ID, or @username."), reply_to=msg.message_id)
            return
        token = f"{chat.id}_{msg.message_id}"
        ANON_PENDING[token] = {"action": "approve", "chat_id": chat.id, "target_id": target.id, "target_name": target.first_name or "User"}
        kb = InlineKeyboardMarkup([[B("𝙥𝙧𝙤𝙫𝙚 𝙊𝙬𝙣𝙚𝙧/𝙖𝙙𝙢𝙞𝙣", f"anonlock:{token}", style="success")]])
        await ctx.bot.send_message(
            chat.id,
            T("<b>⚠️ Anonymous Admin detected.</b>\nOnly the real group owner can approve. Tap to verify."),
            reply_to_message_id=msg.message_id,
            reply_markup=kb,
        )
        return

    if not await _is_full_admin_for_locks(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("⚠️ only admins with <b>Change Group Info</b> permission can use this."), reply_to=msg.message_id)
        return

    target, _ = await resolve_target(update, ctx)
    if not target:
        await say(ctx, chat.id, T("❌ Invalid format. Reply, ID, or @username."), reply_to=msg.message_id)
        return

    name = target.first_name or "User"
    await dbase.approved_add(chat.id, target.id, name)
    await say(ctx, chat.id, T(f"✅ <b>{html.escape(name)}</b> is now approved. Locks won't apply to them."), reply_to=msg.message_id)


async def unapprove_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return

    if user.id == ANON_ADMIN_ID:
        target, _ = await resolve_target(update, ctx)
        if not target:
            await say(ctx, chat.id, T("❌ Invalid format. Reply, ID, or @username."), reply_to=msg.message_id)
            return
        token = f"{chat.id}_{msg.message_id}"
        ANON_PENDING[token] = {"action": "unapprove", "chat_id": chat.id, "target_id": target.id, "target_name": target.first_name or "User"}
        kb = InlineKeyboardMarkup([[B("𝙥𝙧𝙤𝙫𝙚 𝙊𝙬𝙣𝙚𝙧/𝙖𝙙𝙢𝙞𝙣", f"anonlock:{token}", style="success")]])
        await ctx.bot.send_message(
            chat.id,
            T("<b>⚠️ Anonymous Admin detected.</b>\nOnly the real group owner can approve. Tap to verify."),
            reply_to_message_id=msg.message_id,
            reply_markup=kb,
        )
        return

    if not await _is_full_admin_for_locks(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("⚠️ only admins with <b>Change Group Info</b> permission can use this."), reply_to=msg.message_id)
        return

    target, _ = await resolve_target(update, ctx)
    if not target:
        await say(ctx, chat.id, T("❌ Invalid format. Reply, ID, or @username."), reply_to=msg.message_id)
        return

    name = target.first_name or "User"
    removed = await dbase.approved_remove(chat.id, target.id)
    if removed:
        await say(ctx, chat.id, T(f"✅ <b>{html.escape(name)}</b> is no longer approved. Locks will apply to them."), reply_to=msg.message_id)
    else:
        await say(ctx, chat.id, T(f"ℹ️ <b>{html.escape(name)}</b> was not in the approved list."), reply_to=msg.message_id)


async def approved_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return
    approved = await dbase.approved_list(chat.id)
    if not approved:
        await say(ctx, chat.id, T("No approved users in this chat."), reply_to=msg.message_id)
        return
    lines = ["<b>✅ Approved users:</b>"]
    for i, (uid, name) in enumerate(approved.items(), 1):
        lines.append(f"{i}. {html.escape(name)} — <code>{uid}</code>")
    await say(ctx, chat.id, T("\n".join(lines)), reply_to=msg.message_id)


async def locks_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return
    data = await dbase.locks_get(chat.id)
    locks = data.get("locks", set())
    unlocks = data.get("unlocks", set())
    if not locks:
        await say(ctx, chat.id, T("No locks active in this chat."), reply_to=msg.message_id)
        return
    lines = ["<b>🔒 Active locks:</b>"]
    for lt in sorted(locks):
        lines.append(f"• <code>{lt}</code>")
    if "all" in locks and unlocks:
        lines.append("")
        lines.append("<b>🔓 Unlocked exceptions:</b>")
        for lt in sorted(unlocks):
            lines.append(f"• <code>{lt}</code>")
    await say(ctx, chat.id, T("\n".join(lines)), reply_to=msg.message_id)


async def locktypes_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return
    await say(ctx, chat.id, "The available locktypes are:", kb=get_locktypes_kb(), reply_to=msg.message_id)


async def lock_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    qy = update.callback_query
    data = qy.data.split(":")[1]
    desc = LOCK_DESC.get(data, "No description available.")
    bold_title = "".join(
        [chr(ord(c) - 97 + 0x1D68A) if 'a' <= c <= 'z' else c for c in data]
    )
    await qy.answer(f"{bold_title}:\n\n{desc}", show_alert=True)


async def anon_lock_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    qy = update.callback_query
    token = qy.data.split(":", 1)[1]
    st = ANON_PENDING.get(token)
    if not st:
        await qy.answer("This request has expired.", show_alert=True)
        return
    chat_id = st["chat_id"]
    real_owner_id = await _get_real_group_owner_id(ctx, chat_id)

    if qy.from_user.id != real_owner_id and qy.from_user.id != BOT_OWNER_ID:
        await qy.answer("❌ Only the real group owner can approve this!", show_alert=True)
        return

    action = st["action"]
    label = ""

    if action == "lock":
        await _apply_lock(chat_id, st["items"])
        label = ", ".join(st["items"])
        verb = "locked"
    elif action == "unlock":
        await _apply_unlock(chat_id, st["items"])
        label = ", ".join(st["items"])
        verb = "unlocked"
    elif action == "approve":
        await dbase.approved_add(chat_id, st["target_id"], st["target_name"])
        label = st["target_name"]
        verb = "approved"
    elif action == "unapprove":
        await dbase.approved_remove(chat_id, st["target_id"])
        label = st["target_name"]
        verb = "unapproved"
    else:
        await qy.answer("Unknown action.", show_alert=True)
        return

    ANON_PENDING.pop(token, None)
    await qy.edit_message_text(T(f"✅ Approved by owner. <b>{html.escape(label)}</b> {verb}."))


async def _locks_watcher(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    if chat.type == ChatType.PRIVATE:
        return
    if user.is_bot:
        return

    data = await dbase.locks_get(chat.id)
    if not data.get("locks"):
        return

    is_admin = _is_admin_cached(chat.id, user.id)
    if is_admin is None:
        try:
            member = await ctx.bot.get_chat_member(chat.id, user.id)
            is_admin = member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
            _cache_admin(chat.id, user.id, is_admin)
        except TelegramError:
            is_admin = False
            _cache_admin(chat.id, user.id, False)
    if is_admin:
        return

    if user.id in data.get("approved", {}):
        return

    me = ctx.bot_data.get("me")
    if me:
        cache_key = f"bot_perm_{chat.id}"
        bot_can_delete = ctx.bot_data.get(cache_key)
        if bot_can_delete is None:
            try:
                bot_member = await ctx.bot.get_chat_member(chat.id, me.id)
                bot_can_delete = getattr(bot_member, "can_delete_messages", False)
                ctx.bot_data[cache_key] = bot_can_delete
            except TelegramError:
                return
        if not bot_can_delete:
            if ctx.bot_data.get(f"warned_{chat.id}") is not True:
                ctx.bot_data[f"warned_{chat.id}"] = True
                await say(ctx, chat.id, T("⚠️ I need <b>Delete Messages</b> permission to enforce locks. Please promote me!"))
            return

    try:
        if await is_message_locked(ctx, chat.id, msg):
            await msg.delete()
    except TelegramError:
        pass


def register(app):
    dual_command(app, "lock", lock_cmd)
    dual_command(app, "unlock", unlock_cmd)
    dual_command(app, "locks", locks_cmd)
    dual_command(app, "locktypes", locktypes_cmd)
    dual_command(app, "approve", approve_cmd)
    dual_command(app, "unapprove", unapprove_cmd)
    dual_command(app, "approved", approved_cmd)
    app.add_handler(CallbackQueryHandler(lock_callback, pattern=r"^lockinfo:"))
    app.add_handler(CallbackQueryHandler(anon_lock_callback, pattern=r"^anonlock:"))
    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, _locks_watcher), group=10)
    
