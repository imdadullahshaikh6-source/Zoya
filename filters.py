"""Filters module — Rose-style auto replies (Supports Native, Custom, and Rose buttons with colors)."""
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
    ("stop", "Delete all filters in this chat"),
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
    "Supports Native Telegram buttons, <code>[Text ~ URL]</code>, and Rose-style <code>[Text](buttonurl:URL:color)</code>.\n"
    "Colors: <code>:danger</code> (Red), <code>:success</code> (Green), <code>:primary</code> (Blue)"
)


_BTN_RE = re.compile(r"\[([^\[\]~]+?)\s*~\s*([^\[\]]+?)\]")
# ✅ FIX: Ab dono Markdown aur HTML format handle karega
_MD_RE = re.compile(r"\[([^\[\]]+?)\]\(buttonurl:(https?://[^\)]+?)\)")
_HTML_RE = re.compile(r'<a href="buttonurl:(https?://[^"]+?)">([^<]+?)</a>')


def _parse_buttons(text: str | None):
    """Extract custom [Text ~ URL] AND Rose [Text](buttonurl:URL:color) buttons."""
    if not text:
        return text or "", None

    rows = []

    def _process_button(label, url):
        color = None
        # Check karo URL ke end me color tag hai ya nahi
        for c in ["danger", "success", "primary"]:
            if url.endswith(f":{c}"):
                color = c
                url = url[:-len(f":{c}")]  # URL se color tag hata do
                break
        
        btn = {"text": label.strip(), "url": url.strip()}
        if color:
            btn["style"] = color
        rows.append([btn])
        return ""

    def _repl_md(m):
        return _process_button(m.group(1), m.group(2))

    def _repl_html(m):
        return _process_button(m.group(2), m.group(1))

    # Apply Markdown regex
    cleaned = _MD_RE.sub(_repl_md, text)
    # Apply HTML regex
    cleaned = _HTML_RE.sub(_repl_html, cleaned).strip()

    # Custom [Text ~ URL]
    def _repl_custom(m):
        label = m.group(1).strip()
        url = m.group(2).strip()
        if not (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
            return m.group(0)
        rows.append([{"text": label, "url": url}])
        return ""

    cleaned = _BTN_RE.sub(_repl_custom, cleaned).strip()
    return cleaned, (rows or None)


def _extract_native_buttons(reply_msg):
    """Extract Telegram's native inline buttons (like JOIN buttons)."""
    if not reply_msg or not reply_msg.reply_markup:
        return None
    markup = reply_msg.reply_markup
    if not hasattr(markup, 'inline_keyboard') or not markup.inline_keyboard:
        return None
    
    rows = []
    for row in markup.inline_keyboard:
        btn_row = []
        for btn in row:
            if btn.url:
                btn_data = {"text": btn.text, "url": btn.url}
                if hasattr(btn, 'style') and btn.style:
                    btn_data["style"] = btn.style
                btn_row.append(btn_data)
        if btn_row:
            rows.append(btn_row)
    return rows if rows else None


def _kb_from_stored(rows):
    """Build InlineKeyboardMarkup safely."""
    if not rows:
        return None
    
    kb_rows = []
    for row in rows:
        kb_row = []
        for b in row:
            try:
                if "style" in b and b["style"]:
                    btn = InlineKeyboardButton(text=b["text"], url=b["url"], style=b["style"])
                else:
                    btn = InlineKeyboardButton(text=b["text"], url=b["url"])
            except TypeError:
                btn = InlineKeyboardButton(text=b["text"], url=b["url"])
            kb_row.append(btn)
        kb_rows.append(kb_row)
        
    return InlineKeyboardMarkup(kb_rows)


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
    native_btns = _extract_native_buttons(reply)

    if reply.text:
        clean, btns = _parse_buttons(reply.text_html)
        final_btns = (native_btns or []) + (btns or [])
        data.update(type="text", content=clean, buttons=final_btns or None)
        
    elif reply.sticker:
        final_btns = (native_btns or [])
        data.update(type="sticker", content=reply.sticker.file_id, buttons=final_btns or None)
        
    elif reply.photo:
        clean, btns = _parse_buttons(reply.caption_html)
        final_btns = (native_btns or []) + (btns or [])
        data.update(type="photo", content=reply.photo[-1].file_id, caption=clean, buttons=final_btns or None)
        
    elif reply.video:
        clean, btns = _parse_buttons(reply.caption_html)
        final_btns = (native_btns or []) + (btns or [])
        data.update(type="video", content=reply.video.file_id, caption=clean, buttons=final_btns or None)
        
    elif reply.animation:
        clean, btns = _parse_buttons(reply.caption_html)
        final_btns = (native_btns or []) + (btns or [])
        data.update(type="animation", content=reply.animation.file_id, caption=clean, buttons=final_btns or None)
        
    elif reply.audio:
        clean, btns = _parse_buttons(reply.caption_html)
        final_btns = (native_btns or []) + (btns or [])
        data.update(type="audio", content=reply.audio.file_id, caption=clean, buttons=final_btns or None)
        
    else:
        await msg.reply_text("⚠️ Unsupported message type.")
        return

    await dbase.filter_set(chat.id, keyword, data)
    await msg.reply_text(f"✅ Filter <b>{keyword}</b> saved with formatting and buttons.")


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
    msg, chat = update.effective_message, update.effective_chat
    if not msg:
        return

    text = msg.text or msg.caption
    if not text:
        return

    if text.startswith(("/", ".")):
        return

    words = set(re.findall(r"@?\w+", text.lower()))
    if not words:
        return

    for word in words:
        row = await dbase.filter_get(chat.id, word)
        
        if not row and word.startswith("@"):
            row = await dbase.filter_get(chat.id, word[1:])
        
        if not row and not word.startswith("@"):
            row = await dbase.filter_get(chat.id, f"@{word}")

        if not row:
            continue

        kb = _kb_from_stored(row.get("buttons"))
        ftype = row.get("type", "text")
        content = row.get("content", "")
        caption = row.get("caption", "")

        try:
            if ftype == "text":
                await msg.reply_text(content or "", reply_markup=kb, parse_mode=ParseMode.HTML, reply_to_message_id=msg.message_id)
            elif ftype == "sticker":
                await msg.reply_sticker(content, reply_markup=kb, reply_to_message_id=msg.message_id)
            elif ftype == "photo":
                await msg.reply_photo(content, caption=caption or None, reply_markup=kb, parse_mode=ParseMode.HTML, reply_to_message_id=msg.message_id)
            elif ftype == "video":
                await msg.reply_video(content, caption=caption or None, reply_markup=kb, parse_mode=ParseMode.HTML, reply_to_message_id=msg.message_id)
            elif ftype == "animation":
                await msg.reply_animation(content, caption=caption or None, reply_markup=kb, parse_mode=ParseMode.HTML, reply_to_message_id=msg.message_id)
            elif ftype == "audio":
                await msg.reply_audio(content, caption=caption or None, reply_markup=kb, parse_mode=ParseMode.HTML, reply_to_message_id=msg.message_id)
        except Exception as e:
            log.error("filter send failed [%s/%s]: %s", chat.id, word, e)
            try:
                await msg.reply_text(f"❌ Error sending filter: <code>{e}</code>", reply_to_message_id=msg.message_id)
            except:
                pass

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

    app.add_handler(MessageHandler(
        (tg_filters.TEXT | tg_filters.CAPTION) & tg_filters.ChatType.GROUPS,
        trigger_filter
    ), group=-2)
