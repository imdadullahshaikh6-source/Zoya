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
    "invitelink": "Private/public invite links to groups/channels.",
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

def get_locktypes_kb():
    buttons = []
    row = []
    for lt in LOCKTYPES:
        # Capitalize display with Sans-Serif Bold font
        display_name = "".join([chr(ord(c) - 97 + 0x1D68A) if 'a' <= c <= 'z' else c for c in lt])
        # ✅ FIX: InlineKeyboardButton ki jagah B() use kiya (jo style support karta hai)
        row.append(B(display_name, callback_data=f"lockinfo:{lt}", style="primary"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)

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

    if "locks_db" not in ctx.bot_data:
        ctx.bot_data["locks_db"] = {}
    if chat.id not in ctx.bot_data["locks_db"]:
        ctx.bot_data["locks_db"][chat.id] = set()

    for item in items:
        ctx.bot_data["locks_db"][chat.id].add(item)

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
    if "locks_db" not in ctx.bot_data or chat.id not in ctx.bot_data["locks_db"]:
        await say(ctx, chat.id, T("no locks active."), reply_to=msg.message_id)
        return

    for item in items:
        ctx.bot_data["locks_db"][chat.id].discard(item)

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
    await qy.answer()
    data = qy.data.split(":")[1]
    desc = LOCK_DESC.get(data, "No description available.")
    await qy.answer(f"{data}:\n\n{desc}", show_alert=True)

async def _locks_watcher(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE or not user or user.is_bot:
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
                if ctx.bot_data.get(f"warned_{chat.id}") != True:
                    ctx.bot_data[f"warned_{chat.id}"] = True
                    await say(ctx, chat.id, T("⚠️ I need <b>Delete Messages</b> permission to enforce locks. Please promote me!"))
                return
        except TelegramError:
            return

    should_delete = False

    if "all" in active:
        should_delete = True
    else:
        if "text" in active and msg.text:
            should_delete = True
        if "photo" in active and msg.photo:
            should_delete = True
        if "video" in active and msg.video:
            should_delete = True
        if "audio" in active and msg.audio:
            should_delete = True
        if "voice" in active and msg.voice:
            should_delete = True
        if "document" in active and msg.document:
            should_delete = True
        if "sticker" in active and msg.sticker:
            should_delete = True
        if "gif" in active and msg.animation:
            should_delete = True
        if "videonote" in active and msg.video_note:
            should_delete = True
        if "poll" in active and msg.poll:
            should_delete = True
        if "contact" in active and msg.contact:
            should_delete = True
        if "location" in active and msg.location:
            should_delete = True
        if "forward" in active and (msg.forward_date or msg.forward_origin):
            should_delete = True
        if "anonchannel" in active and msg.sender_chat:
            should_delete = True
        if "bot" in active and msg.from_user and msg.from_user.is_bot:
            should_delete = True
        if "album" in active and msg.media_group_id:
            should_delete = True
        if "button" in active and msg.reply_markup:
            should_delete = True

        if msg.entities:
            for e in msg.entities:
                if "url" in active and e.type in ("url", "text_link"):
                    url_text = msg.text[e.offset:e.offset+e.length] if msg.text else ""
                    if "t.me/" not in url_text:
                        should_delete = True
                if "email" in active and e.type == "email":
                    should_delete = True
                if "phone" in active and e.type == "phone_number":
                    should_delete = True
                if "spoiler" in active and e.type == "spoiler":
                    should_delete = True
                if ("emoji" in active or "emojicustom" in active) and e.type == "custom_emoji":
                    should_delete = True
                if "botlink" in active and e.type in ("url", "text_link"):
                    url_text = msg.text[e.offset:e.offset+e.length] if msg.text else ""
                    if "t.me/" in url_text and "bot" in url_text:
                        should_delete = True
                if "invitelink" in active and e.type in ("url", "text_link"):
                    url_text = msg.text[e.offset:e.offset+e.length] if msg.text else ""
                    if "t.me/joinchat" in url_text or "t.me/+" in url_text:
                        should_delete = True

    if should_delete:
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
