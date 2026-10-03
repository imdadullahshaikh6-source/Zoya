"""Locks plugin: lock types to auto-delete specific message types."""
import html
import re

from telegram import Update, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes, MessageHandler, filters, CallbackQueryHandler

from common import B, T, dual_command, say

COMMANDS = [
    ("lock", "Lock message types"),
    ("unlock", "Unlock message types"),
    ("locks", "List currently locked types"),
    ("locktypes", "Show all lockable types"),
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
    "text": "Text messages.",
    "video": "Videos.",
    "videonote": "Video notes (round videos).",
    "voice": "Voice messages."
}


def get_locktypes_kb(back: bool = False):
    buttons = []
    row = []
    for lt in LOCKTYPES:
        display_name = "".join([chr(ord(c) - 97 + 0x1D68A) if 'a' <= c <= 'z' else c for c in lt])
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


def is_message_locked(bot_data: dict, chat_id: int, msg) -> bool:
    """Public helper — returns True if the message should be deleted by locks."""
    active = bot_data.get("locks_db", {}).get(chat_id, set())
    unlocked = bot_data.get("unlocks_db", {}).get(chat_id, set())
    
    # ✅ FIX: Agar koi lock active nahi hai, toh seedha False
    if not active:
        return False

    has_media = _has_media(msg)

    # Agar "all" locked hai, toh sirf exceptions (unlocked) safe hain
    if "all" in active:
        if "text" in unlocked and msg.text and not has_media: return False
        if "photo" in unlocked and msg.photo: return False
        if "video" in unlocked and msg.video: return False
        if "audio" in unlocked and msg.audio: return False
        if "voice" in unlocked and msg.voice: return False
        if "document" in unlocked and msg.document: return False
        if "sticker" in unlocked and msg.sticker: return False
        if "gif" in unlocked and msg.animation: return False
        if "videonote" in unlocked and msg.video_note: return False
        if "poll" in unlocked and msg.poll: return False
        if "contact" in unlocked and msg.contact: return False
        if "location" in unlocked and msg.location: return False
        if "forward" in unlocked and (msg.forward_date or msg.forward_origin): return False
        if "anonchannel" in unlocked and msg.sender_chat: return False
        if "bot" in unlocked and msg.from_user and msg.from_user.is_bot: return False
        if "album" in unlocked and msg.media_group_id: return False
        if "button" in unlocked and msg.reply_markup: return False
        return True

    # No "all" lock — check specific types
    if "text" in active and msg.text and not has_media: return True
    if "photo" in active and msg.photo: return True
    if "video" in active and msg.video: return True
    if "audio" in active and msg.audio: return True
    if "voice" in active and msg.voice: return True
    if "document" in active and msg.document: return True
    if "sticker" in active and msg.sticker: return True
    if "gif" in active and msg.animation: return True
    if "videonote" in active and msg.video_note: return True
    if "poll" in active and msg.poll: return True
    if "contact" in active and msg.contact: return True
    if "location" in active and msg.location: return True
    if "forward" in active and (msg.forward_date or msg.forward_origin): return True
    if "anonchannel" in active and msg.sender_chat: return True
    if "bot" in active and msg.from_user and msg.from_user.is_bot: return True
    if "album" in active and msg.media_group_id: return True
    if "button" in active and msg.reply_markup: return True

    # Entities
    if msg.entities:
        for e in msg.entities:
            if "url" in active and e.type in ("url", "text_link"):
                url_text = msg.text[e.offset:e.offset+e.length] if msg.text else ""
                if "t.me/" not in url_text: return True
            if "email" in active and e.type == "email": return True
            if "phone" in active and e.type == "phone_number": return True
            if "spoiler" in active and e.type == "spoiler": return True
            if ("emoji" in active or "emojicustom" in active) and e.type == "custom_emoji": return True
            if "botlink" in active and e.type in ("url", "text_link"):
                url_text = msg.text[e.offset:e.offset+e.length] if msg.text else ""
                if "t.me/" in url_text and "bot" in url_text: return True
            if "invitelink" in active and e.type in ("url", "text_link"):
                url_text = msg.text[e.offset:e.offset+e.length] if msg.text else ""
                if "t.me/joinchat" in url_text or "t.me/+" in url_text: return True

    return False


async def lock_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return
    try:
        m = await ctx.bot.get_chat_member(chat.id, user.id)
        if m.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await say(ctx, chat.id, T("only admins can use this."), reply_to=msg.message_id)
            return
    except TelegramError:
        return

    if not ctx.args:
        await say(ctx, chat.id, T("usage: /lock <item(s)>"), reply_to=msg.message_id)
        return

    items = [i.lower() for i in ctx.args]
    invalid = [i for i in items if i not in LOCKTYPES and i != "all"]
    if invalid:
        await say(ctx, chat.id, T(f"invalid lock types: {', '.join(invalid)}"), reply_to=msg.message_id)
        return

    ctx.bot_data.setdefault("locks_db", {}).setdefault(chat.id, set())
    ctx.bot_data.setdefault("unlocks_db", {}).setdefault(chat.id, set())

    # ✅ FIX: Agar "all" lock kiya, toh saare purane locks hata do
    if "all" in items:
        ctx.bot_data["locks_db"][chat.id] = {"all"}
        ctx.bot_data["unlocks_db"][chat.id].clear()
    else:
        for item in items:
            ctx.bot_data["locks_db"][chat.id].add(item)
            ctx.bot_data["unlocks_db"][chat.id].discard(item)

    await say(ctx, chat.id, T(f"✅ locked: {', '.join(items)}"), reply_to=msg.message_id)


async def unlock_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return
    try:
        m = await ctx.bot.get_chat_member(chat.id, user.id)
        if m.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await say(ctx, chat.id, T("only admins can use this."), reply_to=msg.message_id)
            return
    except TelegramError:
        return

    if not ctx.args:
        await say(ctx, chat.id, T("usage: /unlock <item(s)>"), reply_to=msg.message_id)
        return

    items = [i.lower() for i in ctx.args]
    invalid = [i for i in items if i not in LOCKTYPES and i != "all"]
    if invalid:
        await say(ctx, chat.id, T(f"invalid lock types: {', '.join(invalid)}"), reply_to=msg.message_id)
        return

    ctx.bot_data.setdefault("locks_db", {}).setdefault(chat.id, set())
    ctx.bot_data.setdefault("unlocks_db", {}).setdefault(chat.id, set())

    # ✅ FIX: Agar "all" unlock kiya, toh saare locks clear kar do
    if "all" in items:
        ctx.bot_data["locks_db"][chat.id].clear()
        ctx.bot_data["unlocks_db"][chat.id].clear()
    else:
        for item in items:
            ctx.bot_data["locks_db"][chat.id].discard(item)
            # Agar "all" locked hai, toh is item ko exception banao
            if "all" in ctx.bot_data["locks_db"][chat.id]:
                ctx.bot_data["unlocks_db"][chat.id].add(item)

    await say(ctx, chat.id, T(f"✅ unlocked: {', '.join(items)}"), reply_to=msg.message_id)


async def locks_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return
    active = ctx.bot_data.get("locks_db", {}).get(chat.id, set())
    if not active:
        await say(ctx, chat.id, T("No locks active in this chat."), reply_to=msg.message_id)
        return
    await say(ctx, chat.id, T(f"🔒 Active locks: {', '.join(active)}"), reply_to=msg.message_id)


async def locktypes_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return
    await say(ctx, chat.id, "The available locktypes are:", kb=get_locktypes_kb(), reply_to=msg.message_id)


async def lock_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    qy = update.callback_query
    data = qy.data.split(":")[1]
    desc = LOCK_DESC.get(data, "No description available.")
    bold_title = "".join([chr(ord(c) - 97 + 0x1D68A) if 'a' <= c <= 'z' else c for c in data])
    await qy.answer(f"{bold_title}:\n\n{desc}", show_alert=True)


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

    active = ctx.bot_data.get("locks_db", {}).get(chat.id, set())
    if not active:
        return

    # Admin Bypass
    try:
        member = await ctx.bot.get_chat_member(chat.id, user.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return
    except TelegramError:
        pass

    # Bot Permission Check
    me = ctx.bot_data.get("me")
    if me:
        try:
            bot_member = await ctx.bot.get_chat_member(chat.id, me.id)
            if not getattr(bot_member, "can_delete_messages", False):
                if ctx.bot_data.get(f"warned_{chat.id}") is not True:
                    ctx.bot_data[f"warned_{chat.id}"] = True
                    await say(ctx, chat.id, T("⚠️ I need <b>Delete Messages</b> permission to enforce locks. Please promote me!"))
                return
        except TelegramError:
            return

    if is_message_locked(ctx.bot_data, chat.id, msg):
        try:
            await msg.delete()
        except TelegramError:
            pass


def register(app):
    dual_command(app, "lock", lock_cmd)
    dual_command(app, "unlock", unlock_cmd)
    dual_command(app, "locks", locks_cmd)
    dual_command(app, "locktypes", locktypes_cmd)
    app.add_handler(CallbackQueryHandler(lock_callback, pattern=r"^lockinfo:"))
    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, _locks_watcher), group=10)
