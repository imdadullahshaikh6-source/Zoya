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
from common import B, T, dual_command, mention, q, say, resolve_target

log = logging.getLogger("guardian")

BOT_OWNER_ID = 8373739674
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

COMMANDS = [
    ("setdelay", "Set Guardian deletion delay"),
    ("guard", "Enable or disable Guardian"),
    ("permit", "Whitelist a user from Guardian"),
    ("unpermit", "Remove a user from the permit list"),
    ("permitlist", "Show permitted users"),
]

MIN_DELAY = 60
MAX_DELAY = 6 * 60 * 60
_DELAY_RE = re.compile(r"^(\d+)\s*([smh])$", re.IGNORECASE)
_NOTE_LIFETIME = 5


def _parse_delay(arg: str):
    m = _DELAY_RE.match((arg or "").strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    mult = {"s": 1, "m": 60, "h": 3600}[unit]
    return n * mult


def _safe_name(user) -> str:
    if not user:
        return "Unknown"
    return html.escape(user.first_name or "User")


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

    if user.id == ANON_ADMIN_ID:
        target, _ = await resolve_target(update, ctx)
        if not target:
            await say(ctx, chat.id, T("<blockquote>❌ Invalid format or no target. Reply, ID, or @username.</blockquote>"), reply_to=msg.message_id)
            return
        target_id = target.id
        target_name = html.escape(target.first_name or "User")
        btn_data = f"anonperm|{chat.id}|{target_id}|{target_name}"
        keyboard = InlineKeyboardMarkup([[B("🟢 𝙥𝙧𝙤𝙫𝙚 𝙤𝙬𝙣𝙚𝙧", btn_data, style="success")]])
        await ctx.bot.send_message(
            chat.id,
            q(T("<b>⚠️ Anonymous Admin detected.</b>\nOnly the real group owner can approve permits. Please tap the button below to verify.")),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=msg.message_id,
            reply_markup=keyboard,
        )
        return

    if not await _is_owner(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ only the <b>group owner</b> or <b>bot owner</b> can use this.</blockquote>"), reply_to=msg.message_id)
        return

    target, _ = await resolve_target(update, ctx)
    if not target:
        await say(ctx, chat.id, T("<blockquote>❌ Invalid format or no target. Reply, ID, or @username.</blockquote>"), reply_to=msg.message_id)
        return

    target_id = target.id
    target_name = html.escape(target.first_name or "User")
    await dbase.guardian_permit_add(chat.id, target_id, target_name)
    mention_txt = f"<a href='tg://user?id={target_id}'>{target_name}</a>"
    await say(ctx, chat.id, T(f"<blockquote>✅ {mention_txt} is now <b>permitted</b> — their edits/media won't be deleted.</blockquote>"), reply_to=msg.message_id)


async def unpermit_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return

    if user.id == ANON_ADMIN_ID:
        target, _ = await resolve_target(update, ctx)
        if not target:
            await say(ctx, chat.id, T("<blockquote>❌ Invalid format or no target. Reply, ID, or @username.</blockquote>"), reply_to=msg.message_id)
            return
        target_id = target.id
        target_name = html.escape(target.first_name or "User")
        btn_data = f"anonunperm|{chat.id}|{target_id}|{target_name}"
        keyboard = InlineKeyboardMarkup([[B("🟢 𝙥𝙧𝙤𝙫𝙚 𝙤𝙬𝙣𝙚𝙧", btn_data, style="success")]])
        await ctx.bot.send_message(
            chat.id,
            q(T("<b>⚠️ Anonymous Admin detected.</b>\nOnly the real group owner can approve. Tap the button below to verify.")),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=msg.message_id,
            reply_markup=keyboard,
        )
        return

    if not await _is_owner(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ only the <b>group owner</b> or <b>bot owner</b> can use this.</blockquote>"), reply_to=msg.message_id)
        return

    target, _ = await resolve_target(update, ctx)
    if not target:
        await say(ctx, chat.id, T("<blockquote>❌ Invalid format or no target. Reply, ID, or @username.</blockquote>"), reply_to=msg.message_id)
        return

    target_id = target.id
    target_name = html.escape(target.first_name or "User")
    removed = await dbase.guardian_permit_remove(chat.id, target_id)
    txt = f"<blockquote>✅ <b>{target_name}</b> is no longer permitted.</blockquote>" if removed else f"<blockquote>ℹ️ <b>{target_name}</b> wasn't in the permit list.</blockquote>"
    await say(ctx, chat.id, T(txt), reply_to=msg.message_id)


async def permitlist_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return

    if user.id == ANON_ADMIN_ID:
        btn_data = f"anonlist|{chat.id}"
        keyboard = InlineKeyboardMarkup([[B("🟢 𝙥𝙧𝙤𝙫𝙚 𝙤𝙬𝙣𝙚𝙧", btn_data, style="success")]])
        await ctx.bot.send_message(
            chat.id,
            q(T("<b>⚠️ Anonymous Admin detected.</b>\nOnly the real group owner can view the permit list. Tap to verify.")),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=msg.message_id,
            reply_markup=keyboard,
        )
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
        mention_txt = f"<a href='tg://user?id={target_id}'>{target_name}</a>"
        await query.edit_message_text(T(f"<blockquote>✅ Approved by owner. {mention_txt} is now <b>permitted</b>.</blockquote>"), parse_mode=ParseMode.HTML)
    elif action == "anonunperm":
        target_id = int(data[2]) if data[2] != "0" else None
        target_name = data[3] if data[3] else None
        removed = await dbase.guardian_permit_remove(chat_id, target_id) if target_id else False
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
    
    if not note_text:
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

    # ✅ FIX: Locks compatibility hataya — dono independent kaam karein
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

    # Sirf edited message ka note, media ka nahi
    if is_edit:
        note = f"<blockquote>🗑️ {_safe_name(user)}'s <b>edited message</b> was deleted.</blockquote>"
    else:
        note = ""

    asyncio.create_task(_delete_after(ctx, chat.id, msg.message_id, delay, note))


def register(app):
    dual_command(app, "setdelay", setdelay_cmd)
    dual_command(app, "guard", guard_cmd)
    dual_command(app, "permit", permit_cmd)
    dual_command(app, "unpermit", unpermit_cmd)
    dual_command(app, "permitlist", permitlist_cmd)
    app.add_handler(CallbackQueryHandler(anon_verify_callback, pattern=r"^anon(perm|unperm|list)\|"))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.ALL, _guardian_watcher), group=2)
