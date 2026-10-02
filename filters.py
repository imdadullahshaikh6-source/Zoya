"""Filters plugin — keyword → auto-reply, per chat.

Commands:
  .filter <keyword> <reply>   — save a text filter (or reply to media with .filter <keyword>)
  .filter                     — list every filter in the chat
  .stop <keyword>             — delete one filter
  .clearfilters               — delete every filter in the chat

Filters can store:
  • plain text replies
  • any media (photo / video / sticker / gif / voice / audio / document)
  • inline buttons via [Label](buttonurl://https://example.com) syntax

All filters are stored in MongoDB per chat and survive restarts."""
import logging
import re

from telegram import Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes, MessageHandler, filters as tg_filters

import database as dbase
from common import (
    B, T, dual_command, esc, mention, parse_buttons, q, require_admin, say, sc,
)

log = logging.getLogger("filters")

HELP_TXT = (
    "<b>🔍 𝙁𝙞𝙡𝙩𝙚𝙧𝙨</b>\n\n"
    "Auto-reply whenever a keyword is said in chat.\n\n"
    "<b>Commands (admin only):</b>\n"
    "• <code>.filter keyword reply text</code> — save a text filter.\n"
    "• <code>.filter keyword</code> (reply to media) — save a media filter.\n"
    "• <code>.filter</code> — list every filter in this chat.\n"
    "• <code>.stop keyword</code> — delete one filter.\n"
    "• <code>.clearfilters</code> — delete every filter.\n\n"
    "<b>Buttons in replies:</b>\n"
    "<code>[Label](buttonurl://https://example.com)</code>\n"
    "<code>[Label](buttonurl://https://example.com:same)</code> — same row\n"
    "<code>[Label](buttonurl://https://example.com:danger)</code> — red button"
)
COMMANDS = [
    ("filter", "Save a filter for a keyword"),
    ("stop", "Delete a filter"),
    ("clearfilters", "Delete every filter in this chat"),
]

MAX_FILTERS = 100


def _media_payload(src_msg) -> dict | None:
    """Turn a replied message into a filter payload (media only)."""
    if src_msg is None:
        return None
    if src_msg.text:
        return None
    if src_msg.photo:
        return {"type": "photo", "content": src_msg.photo[-1].file_id,
                "caption": src_msg.caption or ""}
    if src_msg.video:
        return {"type": "video", "content": src_msg.video.file_id,
                "caption": src_msg.caption or ""}
    if src_msg.animation:
        return {"type": "animation", "content": src_msg.animation.file_id,
                "caption": src_msg.caption or ""}
    if src_msg.sticker:
        return {"type": "sticker", "content": src_msg.sticker.file_id,
                "caption": ""}
    if src_msg.voice:
        return {"type": "voice", "content": src_msg.voice.file_id,
                "caption": src_msg.caption or ""}
    if src_msg.audio:
        return {"type": "audio", "content": src_msg.audio.file_id,
                "caption": src_msg.caption or ""}
    if src_msg.document:
        return {"type": "document", "content": src_msg.document.file_id,
                "caption": src_msg.caption or ""}
    return None


# ───────────── commands ─────────────

async def filter_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        await say(ctx, chat.id, T("filters only work inside groups."))
        return

    args = list(ctx.args or [])

    # No args → list all filters
    if not args:
        rows = await dbase.filter_list(chat.id)
        if not rows:
            await say(ctx, chat.id, T("no filters set in this chat yet."), reply_to=msg.message_id)
            return
        lines = [T("<b>🔍 filters in this chat ({n}):</b>", n=len(rows))]
        for r in rows[:60]:
            lines.append(f"• <code>{esc(r['keyword'])}</code> — <i>{esc(r['type'])}</i>")
        if len(rows) > 60:
            lines.append(T("…and {n} more.", n=len(rows) - 60))
        await say(ctx, chat.id, "\n".join(lines), reply_to=msg.message_id)
        return

    # Permission check for setting/deleting
    if not await require_admin(update, ctx, right="delete_messages"):
        return

    keyword = args[0].lower()
    if len(keyword) < 2 or len(keyword) > 64:
        await say(ctx, chat.id, T("keyword must be 2-64 characters."), reply_to=msg.message_id)
        return

    existing = await dbase.filter_get(chat.id, keyword)
    reply = msg.reply_to_message

    # ── Delete if keyword already exists AND no new content given ──
    if len(args) == 1 and existing and not reply:
        await dbase.filter_delete(chat.id, keyword)
        await say(ctx, chat.id, T("🗑️ deleted filter <code>{k}</code>.", k=esc(keyword)), reply_to=msg.message_id)
        return

    # ── Build payload ──
    if reply and (reply.photo or reply.video or reply.animation or reply.sticker
                  or reply.voice or reply.audio or reply.document):
        media = _media_payload(reply)
        payload = {
            "type": media["type"],
            "content": media["content"],
            "caption": media["caption"] or "",
            "buttons": None,
            "set_by": update.effective_user.id,
        }
    elif len(args) >= 2:
        text = " ".join(args[1:]).strip()
        if not text:
            await say(ctx, chat.id, T("give me something to reply with."), reply_to=msg.message_id)
            return
        clean, kb = parse_buttons(text)
        payload = {
            "type": "text",
            "content": clean,
            "caption": "",
            "buttons": [[{"text": b.text, "url": b.url,
                          "style": (b.api_kwargs or {}).get("style")}
                         for b in row] for row in (kb.inline_keyboard if kb else [])] or None,
            "set_by": update.effective_user.id,
        }
    else:
        await say(ctx, chat.id, T("usage: <code>.filter keyword reply</code> (or reply to media)."), reply_to=msg.message_id)
        return

    # ── Cap filter count ──
    if not existing:
        count = await dbase.filter_count(chat.id)
        if count >= MAX_FILTERS:
            await say(ctx, chat.id, T("this chat has reached the {n}-filter limit.", n=MAX_FILTERS), reply_to=msg.message_id)
            return

    await dbase.filter_set(chat.id, keyword, payload)
    action = "updated" if existing else "saved"
    await say(ctx, chat.id, T("✅ {a} filter <code>{k}</code>", a=action, k=esc(keyword)), reply_to=msg.message_id)


async def stop_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return
    if not await require_admin(update, ctx, right="delete_messages"):
        return
    if not ctx.args:
        await say(ctx, chat.id, T("usage: <code>.stop keyword</code>"), reply_to=msg.message_id)
        return
    keyword = ctx.args[0].lower()
    deleted = await dbase.filter_delete(chat.id, keyword)
    if deleted:
        await say(ctx, chat.id, T("🗑️ deleted filter <code>{k}</code>.", k=esc(keyword)), reply_to=msg.message_id)
    else:
        await say(ctx, chat.id, T("no filter called <code>{k}</code>.", k=esc(keyword)), reply_to=msg.message_id)


async def clearfilters_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return
    if not await require_admin(update, ctx, right="delete_messages"):
        return
    n = await dbase.filter_delete_all(chat.id)
    await say(ctx, chat.id, T("🗑️ cleared <b>{n}</b> filters.", n=n), reply_to=msg.message_id)


# ───────────── auto-reply watcher ─────────────

async def _reply_watcher(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat or chat.type == ChatType.PRIVATE:
        return
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return
    # Skip commands
    if text[0] in ("/", "."):
        return

    lowered = text.lower()
    # Word-boundary match so "hello" doesn't fire inside "othello"
    for row in await dbase.filter_list(chat.id):
        kw = row["keyword"]
        if re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", lowered):
            await _send_reply(ctx, chat.id, row, reply_to=msg.message_id)
            return


async def _send_reply(ctx, chat_id: int, row: dict, reply_to: int | None = None):
    """Send the stored filter response — text, media, buttons all supported."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

    kb = None
    if row.get("buttons"):
        try:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(b["text"], url=b["url"],
                                      api_kwargs={"style": b["style"]} if b.get("style") else {})
                 for b in r]
                for r in row["buttons"]
            ])
        except Exception:
            kb = None

    rp = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
    kind = row.get("type", "text")
    content = row.get("content", "")
    caption = row.get("caption") or ""

    try:
        if kind == "text":
            await ctx.bot.send_message(chat_id, content, reply_markup=kb, reply_parameters=rp)
        elif kind == "photo":
            await ctx.bot.send_photo(chat_id, content, caption=caption or None, reply_markup=kb, reply_parameters=rp)
        elif kind == "video":
            await ctx.bot.send_video(chat_id, content, caption=caption or None, reply_markup=kb, reply_parameters=rp)
        elif kind == "animation":
            await ctx.bot.send_animation(chat_id, content, caption=caption or None, reply_markup=kb, reply_parameters=rp)
        elif kind == "sticker":
            await ctx.bot.send_sticker(chat_id, content, reply_markup=kb, reply_parameters=rp)
        elif kind == "voice":
            await ctx.bot.send_voice(chat_id, content, caption=caption or None, reply_markup=kb, reply_parameters=rp)
        elif kind == "audio":
            await ctx.bot.send_audio(chat_id, content, caption=caption or None, reply_markup=kb, reply_parameters=rp)
        elif kind == "document":
            await ctx.bot.send_document(chat_id, content, caption=caption or None, reply_markup=kb, reply_parameters=rp)
    except TelegramError as e:
        log.warning("filter reply failed: %s", e)


# ───────────── registration ─────────────

def register(app):
    dual_command(app, "filter", filter_cmd)
    dual_command(app, "setfilter", filter_cmd)   # alias
    dual_command(app, "stop", stop_cmd)
    dual_command(app, "clearfilters", clearfilters_cmd)

    # group=3 — runs after command handlers, before clean (99)
    app.add_handler(
        MessageHandler(
            (tg_filters.TEXT | tg_filters.CAPTION) & tg_filters.ChatType.GROUPS,
            _reply_watcher,
        ),
        group=3,
  )
