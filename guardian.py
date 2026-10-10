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
    "• <code>.setdelay 5m</code> — delete <b>both</b> edits & media after 5 min.\n"
    "• <code>.editdelay 5m</code> — delete <b>only edits</b> after 5 min (media stays).\n"
    "• <code>.mediadelay 5m</code> — delete <b>only media</b> after 5 min (edits stay).\n"
    "<i>(range → 1 minute to 6 hours; use <code>off</code> to disable)</i>\n\n"
    "<b>Permit Commands (owner only):</b>\n"
    "• <code>.permit</code> (reply, or ID, or @username) — whitelist a user.\n"
    "• <code>.unpermit</code> (reply, or ID, or @username) — remove a user.\n"
    "• <code>.permitlist</code> — view permitted users.\n"
    "• <code>.guard on</code> / <code>.guard off</code> — enable/disable Guardian."
)

COMMANDS = [
    ("setdelay", "Set deletion delay for both edits & media"),
    ("editdelay", "Set deletion delay for edits only"),
    ("mediadelay", "Set deletion delay for media only"),
    ("guard", "Enable or disable Guardian"),
    ("permit", "Whitelist a user from Guardian"),
    ("unpermit", "Remove a user from the permit list"),
    ("permitlist", "Show permitted users"),
]

MIN_DELAY = 60
MAX_DELAY = 6 * 60 * 60
_DELAY_RE = re.compile(r"^(\d+)\s*([smh])$", re.IGNORECASE)
_NOTE_LIFETIME = 5
_OFF_WORDS = {"off", "0", "stop", "disable", "no"}

_cfg_cache: dict = {}
_CACHE_TTL = 30


def _parse_delay(arg: str):
    m = _DELAY_RE.match((arg or "").strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    mult = {"s": 1, "m": 60, "h": 3600}[unit]
    return n * mult


def _fmt_delay(seconds: int) -> str:
    if not seconds or seconds <= 0:
        return "🔴 OFF"
    if seconds < 3600:
        return f"🟢 {seconds // 60} min"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"🟢 {h}h {m}m" if m else f"🟢 {h}h"


def _safe_name(user) -> str:
    if not user:
        return "Unknown"
    return html.escape(user.first_name or "User")


async def _cached_guardian_get(chat_id: int, use_cache: bool = True):
    import time as _time
    now = _time.time()
    if use_cache:
        cached = _cfg_cache.get(chat_id)
        if cached and (now - cached[0]) < _CACHE_TTL:
            return cached[1]
    cfg = await dbase.guardian_get(chat_id)
    _cfg_cache[chat_id] = (now, cfg)
    return cfg


def _invalidate_cache(chat_id: int):
    _cfg_cache.pop(chat_id, None)


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
    except TelegramError as e:
        log.warning("[guardian] bot_can_guard check failed: %s", e)
        return False
    ok = (m.status == ChatMemberStatus.ADMINISTRATOR
          and bool(getattr(m, "can_delete_messages", False)))
    return ok


# ─────────────────── DELAY COMMANDS ───────────────────

async def _apply_delay(ctx, update: Update, mode: str):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return

    if not await _is_full_admin(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ only full-power admins can use this.</blockquote>"), reply_to=msg.message_id)
        return
    if not await _bot_can_guard(ctx, chat.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ make me an admin with <b>Delete Messages</b> permission first.</blockquote>"), reply_to=msg.message_id)
        return

    usage = {
        "both": "<code>.setdelay 5m</code>",
        "edit": "<code>.editdelay 5m</code>",
        "media": "<code>.mediadelay 5m</code>",
    }[mode]

    if not ctx.args:
        await say(ctx, chat.id, T(f"<blockquote>usage: {usage} — range 1m to 6h, or <code>off</code></blockquote>"), reply_to=msg.message_id)
        return

    arg = ctx.args[0].lower().strip()

    if arg in _OFF_WORDS:
        delay = 0
    else:
        delay = _parse_delay(arg)
        if delay is None or delay < MIN_DELAY or delay > MAX_DELAY:
            await say(ctx, chat.id, T("<blockquote>❌ invalid delay — use <code>1m</code> to <code>6h</code> or <code>off</code></blockquote>"), reply_to=msg.message_id)
            return

    # 🔥 UNIVERSAL: har mode sirf apna field update kare, dusre ko touch na kare
    cfg = await _cached_guardian_get(chat.id, use_cache=False) or {}

    def _eff(key):
        v = cfg.get(key)
        if v is None:
            v = cfg.get("delay_seconds") or 0
        return int(v)

    cur_edit  = _eff("edit_delay_seconds")
    cur_media = _eff("media_delay_seconds")

    if mode == "both":
        if delay == 0:
            await dbase.guardian_set(
                chat.id,
                edit_delay_seconds=0,
                media_delay_seconds=0,
                delay_seconds=0,
                enabled=False,
            )
            _invalidate_cache(chat.id)
            await say(ctx, chat.id, T("<blockquote>✅ Guardian turned <b>OFF</b> — neither edits nor media will be deleted.</blockquote>"), reply_to=msg.message_id)
        else:
            await dbase.guardian_set(
                chat.id,
                edit_delay_seconds=delay,
                media_delay_seconds=delay,
                delay_seconds=delay,
                enabled=True,
            )
            _invalidate_cache(chat.id)
            log.info("[guardian] setdelay both=%ds for chat %s", delay, chat.id)
            await say(ctx, chat.id, T(
                f"<blockquote>✅ Guardian active — <b>both edits & media</b> will be deleted after <b>{_fmt_delay(delay).replace('🟢 ', '')}</b>.</blockquote>"
            ), reply_to=msg.message_id)

    elif mode == "edit":
        new_enabled = (delay > 0) or (cur_media > 0)
        await dbase.guardian_set(
            chat.id,
            edit_delay_seconds=delay,
            enabled=new_enabled,
        )
        _invalidate_cache(chat.id)
        if delay == 0:
            await say(ctx, chat.id, T("<blockquote>✅ Edit deletion turned <b>OFF</b>.</blockquote>"), reply_to=msg.message_id)
        else:
            log.info("[guardian] editdelay=%ds for chat %s", delay, chat.id)
            await say(ctx, chat.id, T(
                f"<blockquote>✅ Edit deletion <b>ON</b> — edits deleted after <b>{_fmt_delay(delay).replace('🟢 ', '')}</b>. Media untouched.</blockquote>"
            ), reply_to=msg.message_id)

    elif mode == "media":
        new_enabled = (delay > 0) or (cur_edit > 0)
        await dbase.guardian_set(
            chat.id,
            media_delay_seconds=delay,
            enabled=new_enabled,
        )
        _invalidate_cache(chat.id)
        if delay == 0:
            await say(ctx, chat.id, T("<blockquote>✅ Media deletion turned <b>OFF</b>.</blockquote>"), reply_to=msg.message_id)
        else:
            log.info("[guardian] mediadelay=%ds for chat %s", delay, chat.id)
            await say(ctx, chat.id, T(
                f"<blockquote>✅ Media deletion <b>ON</b> — media deleted after <b>{_fmt_delay(delay).replace('🟢 ', '')}</b>. Edits untouched.</blockquote>"
            ), reply_to=msg.message_id)


async def setdelay_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _apply_delay(ctx, update, "both")


async def editdelay_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _apply_delay(ctx, update, "edit")


async def mediadelay_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _apply_delay(ctx, update, "media")


async def guard_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return
    if not await _is_full_admin(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("<blockquote>⚠️ only full-power admins can use this.</blockquote>"), reply_to=msg.message_id)
        return
    arg = (ctx.args[0].lower() if ctx.args else "")
    cfg = await _cached_guardian_get(chat.id, use_cache=False) or {}
    if arg not in ("on", "off"):
        state = "🟢 ON" if cfg.get("enabled") else "🔴 OFF"

        def _eff(key):
            v = cfg.get(key)
            if v is None:
                v = cfg.get("delay_seconds") or 0
            return int(v)

        edit_d  = _fmt_delay(_eff("edit_delay_seconds"))
        media_d = _fmt_delay(_eff("media_delay_seconds"))
        await say(ctx, chat.id, T(
            f"<blockquote>Guardian is <b>{state}</b>\n"
            f"• Edits: {edit_d}\n"
            f"• Media: {media_d}\n\n"
            f"usage: <code>.guard on</code> / <code>.guard off</code></blockquote>"
        ), reply_to=msg.message_id)
        return
    enabled = arg == "on"
    await dbase.guardian_set(chat.id, enabled=enabled)
    _invalidate_cache(chat.id)
    await say(ctx, chat.id, T(f"<blockquote>✅ Guardian turned <b>{'ON' if enabled else 'OFF'}</b>.</blockquote>"), reply_to=msg.message_id)


# ─────────────────── PERMIT COMMANDS ───────────────────

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
    _invalidate_cache(chat.id)
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
    _invalidate_cache(chat.id)
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
    cfg = await _cached_guardian_get(chat.id, use_cache=False) or {}
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
        _invalidate_cache(chat_id)
        mention_txt = f"<a href='tg://user?id={target_id}'>{target_name}</a>"
        await query.edit_message_text(T(f"<blockquote>✅ Approved by owner. {mention_txt} is now <b>permitted</b>.</blockquote>"), parse_mode=ParseMode.HTML)
    elif action == "anonunperm":
        target_id = int(data[2]) if data[2] != "0" else None
        target_name = data[3] if data[3] else None
        removed = await dbase.guardian_permit_remove(chat_id, target_id) if target_id else False
        _invalidate_cache(chat_id)
        txt = f"<blockquote>✅ Approved by owner. <b>{target_name or target_id}</b> is no longer permitted.</blockquote>" if removed else f"<blockquote>ℹ️ Approved by owner. <b>{target_name or target_id}</b> wasn't in the list.</blockquote>"
        await query.edit_message_text(T(txt), parse_mode=ParseMode.HTML)
    elif action == "anonlist":
        cfg = await _cached_guardian_get(chat_id, use_cache=False) or {}
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
    log.info("[guardian] ⏳ scheduled delete msg=%s chat=%s in %ds", message_id, chat_id, delay)
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return

    for attempt in range(2):
        try:
            await ctx.bot.delete_message(chat_id, message_id)
            log.info("[guardian] ✅ deleted msg %s in chat %s", message_id, chat_id)
            break
        except TelegramError as e:
            err = str(e).lower()
            if "message to delete not found" in err or "message can't be deleted" in err:
                log.info("[guardian] msg %s already gone", message_id)
                return
            if attempt == 0:
                log.warning("[guardian] delete retry for msg %s: %s", message_id, e)
                await asyncio.sleep(2)
                continue
            log.warning("[guardian] ❌ delete failed for msg %s: %s", message_id, e)
            return
        except Exception as e:
            log.error("[guardian] unexpected delete error for %s: %s", message_id, e)
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


async def _get_guard_delay(ctx, chat, user, is_edit: bool, is_media: bool) -> int:
    """Returns delay in seconds (0 = don't delete)."""
    if not chat or chat.type == ChatType.PRIVATE:
        return 0
    me = ctx.application.bot_data.get("me")
    if me and user and user.id == me.id:
        return 0
    if not user or user.is_bot:
        return 0

    cfg = await _cached_guardian_get(chat.id)
    if not cfg or not cfg.get("enabled"):
        return 0

    legacy = int(cfg.get("delay_seconds") or 0)
    edit_delay  = cfg.get("edit_delay_seconds", None)
    media_delay = cfg.get("media_delay_seconds", None)

    if edit_delay is None:
        edit_delay = legacy if legacy else 0
    if media_delay is None:
        media_delay = legacy if legacy else 0

    edit_delay  = int(edit_delay)
    media_delay = int(media_delay)

    # Permitted user check
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

    # 🔥 Dono applicable delays me se MIN use karo
    applicable = []
    if is_edit and edit_delay >= MIN_DELAY:
        applicable.append(edit_delay)
    if is_media and media_delay >= MIN_DELAY:
        applicable.append(media_delay)

    return min(applicable) if applicable else 0


async def _guardian_watcher(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
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

        delay = await _get_guard_delay(ctx, chat, user, is_edit, is_media)
        log.info("[guardian] msg=%s edit=%s media=%s delay=%ds", msg.message_id, is_edit, is_media, delay)
        if not delay:
            return

        note = ""
        if is_edit:
            note = f"<blockquote>🗑️ {_safe_name(user)}'s <b>edited message</b> was deleted.</blockquote>"

        asyncio.create_task(_delete_after(ctx, chat.id, msg.message_id, delay, note))
    except Exception as e:
        log.exception("[guardian] watcher error: %s", e)


def register(app):
    dual_command(app, "setdelay", setdelay_cmd)
    dual_command(app, "editdelay", editdelay_cmd)
    dual_command(app, "mediadelay", mediadelay_cmd)
    dual_command(app, "guard", guard_cmd)
    dual_command(app, "permit", permit_cmd)
    dual_command(app, "unpermit", unpermit_cmd)
    dual_command(app, "permitlist", permitlist_cmd)
    app.add_handler(CallbackQueryHandler(anon_verify_callback, pattern=r"^anon(perm|unperm|list)\|"))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.ALL, _guardian_watcher), group=3)
    
