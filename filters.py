"""Filters module — Rose-style auto replies."""
import logging
import re

from telegram import (
    ChatMember, InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application, ContextTypes, MessageHandler,
    filters as tg_filters,
)

import database as dbase

log = logging.getLogger("filters")


COMMANDS = [
    ("filter", "Set a new filter (reply to a message)"),
    ("unfilter", "Remove a filter by keyword"),
    ("filters", "List all filters in this chat"),
    ("stop", "Delete every filter in this chat"),
]

HELP_TXT = (
    "<b>🔍 Filters</b>\n\n"
    "Filters send an automatic reply whenever a keyword appears in chat.\n\n"
    "<b>Commands</b>\n"
    "• <code>/filter &lt;keyword&gt;</code> — reply to any message to set it\n"
    "• <code>/unfilter &lt;keyword&gt;</code> — delete one filter\n"
    "• <code>/filters</code> — list all filters\n"
    "• <code>/stop</code> — delete every filter\n\n"
    "<b>Supported content</b>\n"
    "Text, stickers, GIFs, audio, video, photos — anything you can reply to.\n\n"
    "<b>Inline buttons</b>\n"
    "Inside your reply text or caption, write:\n"
    "<code>[Button Label ~ https://example.com]</code>\n"
    "Every such block becomes a clickable button when the filter fires."
)


_BTN_RE = re.compile(r"\[([^\[\]~]+?)\s*~\s*([^\[\]]+?)\]")


def _parse_buttons(text: str | None):
    if not text:
        return text or "", None

    rows = []

    def _repl(m):
        label = m.group(1).strip()
        url = m.group(2).strip()
        if not (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
            return m.group(0)
        rows.append([{"text": label, "url": url}])
        return ""

    cleaned = _BTN_RE.sub(_repl, text).strip()
    return cleaned, (rows or None)


def _kb_from_stored(rows):
    if not rows:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(b["text"], url=b["url"]) for b in row] for row in rows])


def _get_args(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        return ctx.args
    text = update.effective_message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) > 1:
        return [parts[1].strip()]
    return []


async def _is_admin(update: Update) -> bool:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return False
    try:
        member = await chat.get_member(update.effective_user.id)
        return member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    except Exception:
        return False


async def filter_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat

    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("Filters only work inside groups.")
        return

    if not await _is_admin(update):
        await msg.reply_text("❌ Only admins can set filters.")
        return

    args = _get_args(update, ctx)
    if not args:
        await msg.reply_text("⚠️ Usage: <code>/filter keyword</code> — reply to a message.")
        return

    keyword = args[0].strip().lower()
    reply = msg.reply_to_message

    if not reply:
        await msg.reply_text("⚠️ Reply to the message you want me to send as the filter.")
        return

    data = {"type": "text", "content": "", "caption": "", "buttons": None, "set_by": update.effective_user.id}

    if reply.text:
        clean, btns = _parse_buttons(reply.text)
        data.update(type="text", content=clean, buttons=btns)
    elif reply.sticker:
        data.update(type="sticker", content=reply.sticker.file_id)
    elif reply.photo:
        clean, btns = _parse_buttons(reply.caption)
        data.update(type="photo", content=reply.photo[-1].file_id, caption=clean, buttons=btns)
    elif reply.video:
        clean, btns = _parse_buttons(reply.caption)
        data.update(type="video", content=reply.video.file_id, caption=clean, buttons=btns)
    elif reply.animation:
        clean, btns = _parse_buttons(reply.caption)
        data.update(type="animation", content=reply.animation.file_id, caption=clean, buttons=btns)
    elif reply.audio:
        clean, btns = _parse_buttons(reply.caption)
        data.update(type="audio", content=reply.audio.file_id, caption=clean, buttons=btns)
    else:
        await msg.reply_text("⚠️ Unsupported message type.")
        return

    await dbase.filter_set(chat.id, keyword, data)
    await msg.reply_text(f"✅ Filter <b>{keyword}</b> saved.")


async def unfilter_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not await _is_admin(update):
        await msg.reply_text("❌ Only admins can remove filters.")
        return
    args = _get_args(update, ctx)
    if not args:
        await msg.reply_text("⚠️ Usage: <code>/unfilter keyword</code>")
        return
    keyword = args[0].strip().lower()
    deleted = await dbase.filter_delete(chat.id, keyword)
    if deleted:
        await msg.reply_text(f"🗑 Filter <b>{keyword}</b> removed.")
    else:
        await msg.reply_text(f"❓ No filter named <b>{keyword}</b> in this chat.")


async def list_filters_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    rows = await dbase.filter_list(chat.id)
    if not rows:
        await msg.reply_text("No filters set in this chat yet.")
        return
    lines = [f"• <code>{r['keyword']}</code>  <i>({r.get('type', 'text')})</i>" for r in rows]
    header = f"<b>🔍 Filters in this chat — {len(rows)}</b>\n\n"
    await msg.reply_text(header + "\n".join(lines))


async def stop_filters_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not await _is_admin(update):
        await msg.reply_text("❌ Only admins can wipe filters.")
        return
    n = await dbase.filter_delete_all(chat.id)
    if n:
        await msg.reply_text(f"🗑 Deleted <b>{n}</b> filter(s) from this chat.")
    else:
        await msg.reply_text("There were no filters to delete.")


async def trigger_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # 🔍 DEBUG LINE
    print(f"🔍 TRIGGER CALLED: {update.effective_message.text}")
    
    msg, chat = update.effective_message, update.effective_chat
    if not msg:
        return

    text = msg.text or msg.caption
    if not text:
        return

    if text.startswith(("/", ".")):
        return

    words = set(re.findall(r"\b\w+\b", text.lower()))
    if not words:
        return

    for word in words:
        row = await dbase.filter_get(chat.id, word)
        if not row:
            continue

        kb = _kb_from_stored(row.get("buttons"))
        ftype = row.get("type", "text")
        content = row.get("content", "")
        caption = row.get("caption", "")

        try:
            if ftype == "text":
                await msg.reply_text(content or "", reply_markup=kb, parse_mode=ParseMode.HTML, do_quote=True)
            elif ftype == "sticker":
                await msg.reply_sticker(content, reply_markup=kb, do_quote=True)
            elif ftype == "photo":
                await msg.reply_photo(content, caption=caption or None, reply_markup=kb, parse_mode=ParseMode.HTML, do_quote=True)
            elif ftype == "video":
                await msg.reply_video(content, caption=caption or None, reply_markup=kb, parse_mode=ParseMode.HTML, do_quote=True)
            elif ftype == "animation":
                await msg.reply_animation(content, caption=caption or None, reply_markup=kb, parse_mode=ParseMode.HTML, do_quote=True)
            elif ftype == "audio":
                await msg.reply_audio(content, caption=caption or None, reply_markup=kb, parse_mode=ParseMode.HTML, do_quote=True)
        except Exception as e:
            log.error("filter send failed [%s/%s]: %s", chat.id, word, e)

        break


def register(app: Application):
    app.add_handler(MessageHandler(
        tg_filters.Regex(r"^[./]filter(?:\s+(.+))?$") & tg_filters.ChatType.GROUPS,
        filter_cmd
    ), group=0)
    
    app.add_handler(MessageHandler(
        tg_filters.Regex(r"^[./]unfilter(?:\s+(.+))?$") & tg_filters.ChatType.GROUPS,
        unfilter_cmd
    ), group=0)
    
    app.add_handler(MessageHandler(
        tg_filters.Regex(r"^[./]filters$") & tg_filters.ChatType.GROUPS,
        list_filters_cmd
    ), group=0)
    
    app.add_handler(MessageHandler(
        tg_filters.Regex(r"^[./]stop$") & tg_filters.ChatType.GROUPS,
        stop_filters_cmd
    ), group=0)

    # ⚠️ CHANGE: group=-2 taaki ye sabse pehle chale
    app.add_handler(MessageHandler(
        (tg_filters.TEXT | tg_filters.CAPTION) & tg_filters.ChatType.GROUPS,
        trigger_filter
    ), group=-2)
