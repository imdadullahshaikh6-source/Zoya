"""Filters module — Rose-style auto replies (Rose buttons + colors)."""
import html
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
    "<b>Commands</b>\n"
    "• <code>/filter &lt;keyword&gt;</code> — reply to a message to set it\n"
    "• <code>/unfilter &lt;keyword&gt;</code> — delete one filter\n"
    "• <code>/filters</code> — list filters\n"
    "• <code>/stop</code> — delete all filters\n\n"
    "<b>Button formats supported</b>\n"
    "• <code>[Text ~ URL]</code>\n"
    "• <code>[Text](buttonurl:URL)</code>\n"
    "• <code>[Text](buttonurl:URL:success)</code> — colors: danger, success, primary"
)

# Simplified regex — direct raw text pe chalega (HTML-free)
_BTN_URL_RE = re.compile(r'\[([^\[\]]+?)\]\s*\(\s*(?:buttonurl:)?\s*([^\)]+?)\s*\)')
_TILDE_RE = re.compile(r'\[([^\[\]~]+?)\s*~\s*([^\[\]]+?)\]')


def _clean_url(raw: str):
    """Return (clean_url, color_or_None) from a messy URL chunk."""
    # Strip HTML tags
    cleaned = re.sub(r'<[^>]+>', '', raw or "")
    # Unescape HTML entities
    cleaned = html.unescape(cleaned).strip()
    # Strip any leading buttonurl:
    if cleaned.lower().startswith("buttonurl:"):
        cleaned = cleaned[len("buttonurl:"):].strip()
    # Strip trailing noise (spaces, newlines, invisible chars)
    cleaned = cleaned.strip().rstrip('.,;:')

    # Extract color suffix via regex (more robust than endswith)
    color = None
    m = re.search(r':(danger|success|primary)\s*$', cleaned, re.IGNORECASE)
    if m:
        color = m.group(1).lower()
        cleaned = cleaned[:m.start()].strip()

    return cleaned, color


def _parse_buttons(text: str | None):
    """Extract buttons from raw text. Returns (clean_text, buttons_or_None)."""
    if not text:
        return text or "", None

    log.info("🔍 RAW INPUT: %r", text)

    rows = []

    def _add(label, raw_url):
        url, color = _clean_url(raw_url)
        log.info("   └─ label=%r | url=%r | color=%r", label, url, color)
        if not url.startswith(("http://", "https://", "tg://")):
            log.info("   └─ ❌ Invalid URL, skipping")
            return False
        btn = {"text": label.strip(), "url": url}
        if color:
            btn["style"] = color
        rows.append([btn])
        return True

    # 1. Match [Text](buttonurl:URL) or [Text](URL)
    def _repl_md(m):
        return "" if _add(m.group(1), m.group(2)) else m.group(0)

    cleaned = _BTN_URL_RE.sub(_repl_md, text)

    # 2. Match [Text ~ URL]
    def _repl_tilde(m):
        return "" if _add(m.group(1), m.group(2)) else m.group(0)

    cleaned = _TILDE_RE.sub(_repl_tilde, cleaned)

    # Strip empty lines
    cleaned = "\n".join(line for line in cleaned.splitlines() if line.strip()).strip()

    log.info("✅ Parsed %d button(s) | cleaned text: %r", len(rows), cleaned)
    return cleaned, (rows or None)


def _extract_native_buttons(reply_msg):
    if not reply_msg or not reply_msg.reply_markup:
        return None
    markup = reply_msg.reply_markup
    if not hasattr(markup, "inline_keyboard") or not markup.inline_keyboard:
        return None
    rows = []
    for row in markup.inline_keyboard:
        btn_row = []
        for btn in row:
            if btn.url:
                data = {"text": btn.text, "url": btn.url}
                style = getattr(btn, "style", None)
                if style:
                    data["style"] = style
                btn_row.append(data)
        if btn_row:
            rows.append(btn_row)
    return rows or None


def _kb_from_stored(rows):
    if not rows:
        return None
    kb_rows = []
    for row in rows:
        kb_row = []
        for b in row:
            try:
                if b.get("style"):
                    kb_row.append(InlineKeyboardButton(text=b["text"], url=b["url"], style=b["style"]))
                else:
                    kb_row.append(InlineKeyboardButton(text=b["text"], url=b["url"]))
            except TypeError:
                kb_row.append(InlineKeyboardButton(text=b["text"], url=b["url"]))
        kb_rows.append(kb_row)
    return InlineKeyboardMarkup(kb_rows)


def _get_args(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        return ctx.args
    parts = (update.effective_message.text or "").split(maxsplit=1)
    return [parts[1].strip()] if len(parts) > 1 else []


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
        await msg.reply_text("⚠️ Reply to the message you want to send as the filter.")
        return

    log.info("=" * 50)
    log.info("📝 FILTER '%s' DEBUG", keyword)
    log.info("reply.text        = %r", reply.text)
    log.info("reply.caption     = %r", reply.caption)
    log.info("reply.text_html   = %r", reply.text_html)
    log.info("reply.caption_html= %r", reply.caption_html)
    log.info("reply.reply_markup= %r", reply.reply_markup)
    log.info("=" * 50)

    data = {"type": "text", "content": "", "caption": "", "buttons": None,
            "set_by": update.effective_user.id}
    native_btns = _extract_native_buttons(reply)

    # Use raw text (reply.text / reply.caption) NOT _html versions
    if reply.text:
        clean, btns = _parse_buttons(reply.text)
        # ✅ Agar custom buttons mile toh native IGNORE karo
        final_btns = btns if btns else native_btns
        data.update(type="text", content=clean, buttons=final_btns)
    elif reply.sticker:
        data.update(type="sticker", content=reply.sticker.file_id, buttons=native_btns)
    elif reply.photo:
        clean, btns = _parse_buttons(reply.caption or "")
        final_btns = btns if btns else native_btns
        data.update(type="photo", content=reply.photo[-1].file_id, caption=clean, buttons=final_btns)
    elif reply.video:
        clean, btns = _parse_buttons(reply.caption or "")
        final_btns = btns if btns else native_btns
        data.update(type="video", content=reply.video.file_id, caption=clean, buttons=final_btns)
    elif reply.animation:
        clean, btns = _parse_buttons(reply.caption or "")
        final_btns = btns if btns else native_btns
        data.update(type="animation", content=reply.animation.file_id, caption=clean, buttons=final_btns)
    elif reply.audio:
        clean, btns = _parse_buttons(reply.caption or "")
        final_btns = btns if btns else native_btns
        data.update(type="audio", content=reply.audio.file_id, caption=clean, buttons=final_btns)
    else:
        await msg.reply_text("⚠️ Unsupported message type.")
        return

    log.info("💾 FINAL BUTTONS: %s", data["buttons"])
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
    if await dbase.filter_delete(chat.id, keyword):
        await msg.reply_text(f"🗑 Filter <b>{keyword}</b> removed.")
    else:
        await msg.reply_text(f"❓ No filter named <b>{keyword}</b>.")


async def list_filters_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    rows = await dbase.filter_list(chat.id)
    if not rows:
        await msg.reply_text("No filters set.")
        return
    lines = [f"• <code>{r['keyword']}</code>  <i>({r.get('type', 'text')})</i>" for r in rows]
    await msg.reply_text(f"<b>🔍 Filters — {len(rows)}</b>\n\n" + "\n".join(lines))


async def stop_filters_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not await _is_admin(update):
        await msg.reply_text("❌ Only admins can wipe filters.")
        return
    n = await dbase.filter_delete_all(chat.id)
    await msg.reply_text(f"🗑 Deleted {n} filter(s)." if n else "No filters to delete.")


async def trigger_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not msg:
        return
    text = msg.text or msg.caption
    if not text or text.startswith(("/", ".")):
        return

    words = set(re.findall(r"@?\w+", text.lower()))
    for word in words:
        row = await dbase.filter_get(chat.id, word) or \
              (word.startswith("@") and await dbase.filter_get(chat.id, word[1:])) or \
              (not word.startswith("@") and await dbase.filter_get(chat.id, f"@{word}"))
        if not row:
            continue

        log.info("🎯 Trigger: %r | buttons=%s", word, row.get("buttons"))
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
        break


def register(app: Application):
    app.add_handler(MessageHandler(tg_filters.Regex(r"^[./]filter(?:\s+(.+))?$") & tg_filters.ChatType.GROUPS, filter_cmd), group=0)
    app.add_handler(MessageHandler(tg_filters.Regex(r"^[./]unfilter(?:\s+(.+))?$") & tg_filters.ChatType.GROUPS, unfilter_cmd), group=0)
    app.add_handler(MessageHandler(tg_filters.Regex(r"^[./]filters$") & tg_filters.ChatType.GROUPS, list_filters_cmd), group=0)
    app.add_handler(MessageHandler(tg_filters.Regex(r"^[./]stop$") & tg_filters.ChatType.GROUPS, stop_filters_cmd), group=0)
    app.add_handler(MessageHandler((tg_filters.TEXT | tg_filters.CAPTION) & tg_filters.ChatType.GROUPS, trigger_filter), group=-2)
