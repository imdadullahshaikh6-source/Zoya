"""Guardian — anti-edit + anti-media defender with delayed deletion."""
import asyncio
import html
import logging
import re

from telegram import Update, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes, MessageHandler, filters, CallbackQueryHandler

import database as dbase
from common import B, T, dual_command, mention, say

log = logging.getLogger("guardian")

# ⚠️ APNI TELEGRAM ID YAHAN DAALEIN (numeric, e.g. 123456789)
BOT_OWNER_ID = 123456789  # <-- yahan apna ID daalein

# Telegram's official Anonymous Admin Bot ID — ise change na karein
ANON_ADMIN_ID = 1087968824

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
    "• <code>.permit</code> (reply, or ID, or @username) — whitelist a user.\n"
    "• <code>.unpermit</code> (reply, or ID, or @username) — remove a user.\n"
    "• <code>.permitlist</code> — view permitted users.\n"
    "• <code>.guard on</code> / <code>.guard off</code> — enable/disable Guardian."
)

# ✅ IMPORTANT: bot.py line ~253 pe `mod.COMMANDS` use hota hai. Isliye ye chahiye.
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
_NOTE_LIFETIME = 5       # seconds


def _parse_delay(arg: str):
    m = _DELAY_RE.match((arg or "").strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    mult = {"s": 1, "m": 60, "h": 3600}[unit]
    return n * mult


def _safe_mention(user) -> str:
    if not user:
        return "Unknown"
    name = html.escape(user.first_name or "User")
    return f"<a href='tg://user?id={user.id}'>{name}</a>"


async def _get_real_group_owner_id(ctx, chat_id: int):
    try:
        admins = await ctx.bot.get_chat_administrators(chat_id)
        for admin in admins:
            if admin.status == ChatMemberStatus.OWNER:
                return admin.user.id
    except TelegramError as e:
        log.warning("[guardian] failed to get owner id: %s", e)
    return None


async def _is_owner(ctx, chat_id: int, user_id: int) -> bool:
    if user_id == BOT_OWNER_ID:
        return True
    try:
        m = await ctx.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    return m.status == ChatMemberStatus.OWNER


async def _is_full_admin(ctx, chat_id: int, user_id: int) -> bool:
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

    # 🔹 Anonymous Admin Detection
    if user.id == ANON_ADMIN_ID:
        target_id, target_name = None, None
        if msg.reply_to_message and msg.reply_to_message.from_user:
            target_id = msg.reply_to_message.from_user.id
            target_name = html.escape(msg.reply_to_message.from_user.first_name or "User")
        elif ctx.args:
            arg = ctx.args[0].strip()
            if arg.isdigit():
                target_id = int(arg)
                target_name = f"User {target_id}"
            elif arg.startswith('@'):
                target_name = html.escape(arg)
                target_id = 0
            else:
                await say(ctx, chat.id, T("<blockquote>❌ Invalid format. Reply, ID, or @username.</blockquote>"), reply_to=msg.message_id)
                return
        else:
            await say(ctx, chat.id, T("<blockquote>usage: reply, or <code>.permit 12345678</code> / <code>.permit @username</code></blockquote>"), reply_to=msg.message_id)
            return

        btn_data = f"anonperm|{chat.id}|{target_id}|{target_name}"
        # ✅ Green button (style="success")
        keyboard = InlineKeyboardMarkup([[B("🟢 I am the Group Owner", btn_data, style="success")]])
        await say(ctx, chat.id, T("<blockquote>⚠️ <b>Anonymous Admin detected.</b>\nOnly the real group owner can approve permits. Please tap the button below to verify.</blockquote>"), reply_to=msg.message_id, reply_markup=keyboard)
        return

    if not await _is_owner(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ only the <b>group owner</b> or <b>bot owner</b> can use this.</blockquote>"), reply_to=msg.message_id)
        return

    target_id, target_name = None, None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_id = msg.reply_to_message.from_user.id
        target_name = html.escape(msg.reply_to_message.from_user.first_name or "User")
    elif ctx.args:
        arg = ctx.args[0].strip()
        if arg.isdigit():
            target_id = int(arg)
            target_name = f"User {target_id}"
        elif arg.startswith('@'):
            target_name = html.escape(arg)
            target_id = 0
        else:
            await say(ctx, chat.id, T("<blockquote>❌ Invalid format. Reply, ID, or @username.</blockquote>"), reply_to=msg.message_id)
            return
    else:
        await say(ctx, chat.id, T("<blockquote>usage: reply, or <code>.permit 12345678</code> / <code>.permit @username</code></blockquote>"), reply_to=msg.message_id)
        return

    await dbase.guardian_permit_add(chat.id, target_id, target_name)
    mention_txt = f"<a href='tg://user?id={target_id}'>{target_name}</a>" if target_id and target_id != 0 else target_name
    await say(ctx, chat.id, T(f"<blockquote>✅ {mention_txt} is now <b>permitted</b> — their edits/media won't be deleted.</blockquote>"), reply_to=msg.message_id)


async def unpermit_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return

    # 🔹 Anonymous Admin Detection
    if user.id == ANON_ADMIN_ID:
        target_id, target_name = None, None
        if msg.reply_to_message and msg.reply_to_message.from_user:
            target_id = msg.reply_to_message.from_user.id
        elif ctx.args:
            arg = ctx.args[0].strip()
            if arg.isdigit():
                target_id = int(arg)
            elif arg.startswith('@'):
                target_name = html.escape(arg)
            else:
                await say(ctx, chat.id, T("<blockquote>❌ Invalid format. Reply, ID, or @username.</blockquote>"), reply_to=msg.message_id)
                return
        else:
            await say(ctx, chat.id, T("<blockquote>usage: reply, or <code>.unpermit 12345678</code> / <code>.unpermit @username</code></blockquote>"), reply_to=msg.message_id)
            return

        btn_data = f"anonunperm|{chat.id}|{target_id or 0}|{target_name or ''}"
        keyboard = InlineKeyboardMarkup([[B("🟢 I am the Group Owner", btn_data, style="success")]])
        await say(ctx, chat.id, T("<blockquote>⚠️ <b>Anonymous Admin detected.</b>\nOnly the real group owner can approve. Tap to verify.</blockquote>"), reply_to=msg.message_id, reply_markup=keyboard)
        return

    if not await _is_owner(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ only the <b>group owner</b> or <b>bot owner</b> can use this.</blockquote>"), reply_to=msg.message_id)
        return

    target_id, target_name = None, None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_id = msg.reply_to_message.from_user.id
    elif ctx.args:
        arg = ctx.args[0].strip()
        if arg.isdigit():
            target_id = int(arg)
        elif arg.startswith('@'):
            target_name = html.escape(arg)
        else:
            await say(ctx, chat.id, T("<blockquote>❌ Invalid format. Reply, ID, or @username.</blockquote>"), reply_to=msg.message_id)
            return
    else:
        await say(ctx, chat.id, T("<blockquote>usage: reply, or <code>.unpermit 12345678</code> / <code>.unpermit @username</code></blockquote>"), reply_to=msg.message_id)
        return

    removed = False
    if target_id:
        removed = await dbase.guardian_permit_remove(chat.id, target_id)
    elif target_name:
        cfg = await dbase.guardian_get(chat.id) or {}
        users = cfg.get("permitted_users", [])
        new_users = [u for u in users if u.get("name") != target_name]
        if len(new_users) != len(users):
            await dbase.guardian_set(chat.id, permitted_users=new_users)
            removed = True

    txt = f"<blockquote>✅ <b>{target_name or target_id}</b> is no longer permitted.</blockquote>" if removed else f"<blockquote>ℹ️ <b>{target_name or target_id}</b> wasn't in the permit list.</blockquote>"
    await say(ctx, chat.id, T(txt), reply_to=msg.message_id)


async def permitlist_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return

    # 🔹 Anonymous Admin Detection
    if user.id == ANON_ADMIN_ID:
        btn_data = f"anonlist|{chat.id}"
        keyboard = InlineKeyboardMarkup([[B("🟢 I am the Group Owner", btn_data, style="success")]])
        await say(ctx, chat.id, T("<blockquote>⚠️ <b>Anonymous Admin detected.</b>\nOnly the real group owner can view the permit list. Tap to verify.</blockquote>"), reply_to=msg.message_id, reply_markup=keyboard)
        return

    if not await _is_owner(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ only the <b>group owner</b> or <b>bot owner</b> can use this.</blockquote>"), reply_to=msg.message_id)
        return

    cfg = await dbase.guardian_get(chat.id) or {}
    users = cfg.get("permitted_users", [])
    if not users:
        await say(ctx, chat.id, T("<blockquote>no permitted users in this chat.</blockquote>"), reply_to=msg.message_id)
        return

    lines = ["<b>🛡 Permitted users:</b>"]
    for i, u in enumerate(users, 1):
        safe_name = html.escape(str(u.get('name', 'Unknown')))
        lines.append(f"{i}. {safe_name} — <code>{u.get('id', 'N/A')}</code>")
    text_body = "\n".join(lines)
    await say(ctx, chat.id, T(f"<blockquote>{text_body}</blockquote>"), reply_to=msg.message_id)


# ───────────── anonymous callback handler ─────────────

async def anon_verify_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data.split("|")
    action = data[0]
    chat_id = int(data[1])

    real_owner_id = await _get_real_group_owner_id(ctx, chat_id)

    if query.from_user.id != real_owner_id and query.from_user.id != BOT_OWNER_ID:
        await query.answer("❌ Only the real group owner can approve this!", show_alert=True)
        return

    if action == "anonperm":
        target_id = int(data[2])
        target_name = data[3]
        await dbase.guardian_permit_add(chat_id, target_id, target_name)
        mention_txt = f"<a href='tg://user?id={target_id}'>{target_name}</a>" if target_id and target_id != 0 else target_name
        await query.edit_message_text(T(f"<blockquote>✅ Approved by owner. {mention_txt} is now <b>permitted</b>.</blockquote>"), parse_mode=ParseMode.HTML)

    elif action == "anonunperm":
        target_id = int(data[2]) if data[2] != "0" else None
        target_name = data[3] if data[3] else None
        removed = False
        if target_id:
            removed = await dbase.guardian_permit_remove(chat_id, target_id)
        elif target_name:
            cfg = await dbase.guardian_get(chat_id) or {}
            users = cfg.get("permitted_users", [])
            new_users = [u for u in users if u.get("name") != target_name]
            if len(new_users) != len(users):
                await dbase.guardian_set(chat_id, permitted_users=new_users)
                removed = True
        txt = f"<blockquote>✅ Approved by owner. <b>{target_name or target_id}</b> is no longer permitted.</blockquote>" if removed else f"<blockquote>ℹ️ Approved by owner. <b>{target_name or target_id}</b> wasn't in the list.</blockquote>"
        await query.edit_message_text(T(txt), parse_mode=ParseMode.HTML)

    elif action == "anonlist":
        cfg = await dbase.guardian_get(chat_id) or {}
        users = cfg.get("permitted_users", [])
        if not users:
            await query.edit_message_text(T("<blockquote>no permitted users in this chat.</blockquote>"), parse_mode=ParseMode.HTML)
            return
        lines = ["<b>🛡 Permitted users:</b>"]
        for i, u in enumerate(users, 1):
            safe_name = html.escape(str(u.get('name', 'Unknown')))
            lines.append(f"{i}. {safe_name} — <code>{u.get('id', 'N/A')}</code>")
        text_body = "\n".join(lines)
        await query.edit_message_text(T(f"<blockquote>{text_body}</blockquote>"), parse_mode=ParseMode.HTML)


# ───────────── watcher ─────────────

def _msg_has_media(msg) -> bool:
    return bool(
        msg.photo or msg.video or msg.video_note
        or msg.voice or msg.audio or msg.document
        or msg.animation or msg.sticker
    )


async def _delete_after(ctx, chat_id: int, message_id: int, delay: int, note_text: str):
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
    if not chat or chat.type == ChatType.PRIVATE:
        return 0
    me = ctx.application.bot_data.get("me")
    if me and user and user.id == me.id:
        return 0
    if not user or user.is_bot:
        return 0
    cfg = await dbase.guardian_get(chat.id)
    if not cfg or not cfg.get("enabled"):
        return 0
    delay = int(cfg.get("delay_seconds") or 0)
    if delay < MIN_DELAY:
        return 0

    permitted = cfg.get("permitted_users", [])
    for u in permitted:
        if u.get("id") and u.get("id") == user.id:
            return 0
        u_name = u.get("name", "")
        if u_name.startswith('@') and user.username:
            if u_name.lower() == f"@{user.username.lower()}":
                return 0

    if not await _bot_can_guard(ctx, chat.id):
        return 0
    return delay


async def _guardian_watcher(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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

    delay = await _should_guard(ctx, chat, user)
    if not delay:
        return

    if is_edit:
        body = f"🗑️ {_safe_mention(user)}'s <b>edited message</b> was deleted."
    else:
        body = f"🗑️ {_safe_mention(user)}'s <b>media</b> was deleted."
    note = f"<blockquote>{body}</blockquote>"

    asyncio.create_task(_delete_after(ctx, chat.id, msg.message_id, delay, note))


# ───────────── registration ─────────────

def register(app):
    dual_command(app, "setdelay", setdelay_cmd)
    dual_command(app, "guard", guard_cmd)
    dual_command(app, "permit", permit_cmd)
    dual_command(app, "unpermit", unpermit_cmd)
    dual_command(app, "permitlist", permitlist_cmd)

    # ✅ FIXED pattern — sirf anonperm/anunperm/anonlist match karega,
    # ban.py ke "anonmod:" se c
