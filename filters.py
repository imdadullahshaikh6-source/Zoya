"""Filters module — Rose-style auto replies.

Ab filters ENTITY-based save hote hain (text + entities), HTML round-trip nahi.
Isse Telegram ka har format as-it-is save/send hota hai:
bold, italic, underline, strike, spoiler, code, pre (language), links, mentions,
hashtags, CUSTOM EMOJI, normal + EXPANDABLE blockquote, date/time, etc.
Media: photo, video, animation(GIF), audio, voice, video note, document, sticker
(spoiler media + caption-above-media bhi preserved).
Rich Messages (Bot API 10.1+, jaise "Meet Zoya" wala divider/pull-quote post) bhi save hote hain.
Buttons: native inline buttons + Rose-style
[Text](buttonurl://URL[:same][:danger|:success|:primary][:custom_emoji_id])
+ [Text](buttonurl://disabled) + [Text ~ URL].
Purane (HTML wale) saved filters bhi chalte rahenge.
"""
import copy
import logging
import re

from telegram import (
    ChatMember, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, CallbackQueryHandler, ContextTypes, MessageHandler,
    filters as tg_filters,
)

import database as dbase
from common import (
    get_rich_message, make_button, normalize_url, parse_button_tail, parse_buttons, send_rich,
)

log = logging.getLogger("filters")


COMMANDS = [
    ("filter", "Set a new filter (reply to a message)"),
    ("unfilter", "Remove a filter by keyword"),
    ("filters", "List all filters in this chat"),
    ("unfilterall", "Delete ALL filters in this chat (with confirmation)"),
]

HELP_TXT = (
    "<b>🔍 Filters</b>\n\n"
    "Filters send an automatic reply whenever a keyword appears in chat.\n\n"
    "<b>Commands</b>\n"
    "• <code>/filter &lt;keyword&gt;</code> — reply to any message to set it\n"
    "• <code>/unfilter &lt;keyword&gt;</code> — delete one filter\n"
    "• <code>/filters</code> — list all filters\n"
    "• <code>/unfilterall</code> — delete every filter (asks for confirmation)\n\n"
    "<b>Supported content</b>\n"
    "Text, photo, video, GIF, audio, voice, video note, document, sticker.\n"
    "Every Telegram format is kept exactly as you wrote it: bold, italic, underline, "
    "strike, spoiler, code, pre, links, custom emoji, quote &amp; expandable quote.\n\n"
    "<b>Inline buttons</b>\n"
    "Native Telegram buttons, <code>[Text ~ URL]</code>, and Rose-style "
    "<code>[Text](buttonurl://URL)</code>\n"
    "Same row: <code>:same</code> — Colors: <code>:danger</code> (Red), "
    "<code>:success</code> (Green), <code>:primary</code> (Blue)\n"
    "Custom emoji icon: add its id, e.g. "
    "<code>[Join](buttonurl://https://t.me/x:danger:5368324170671202286)</code>\n"
    "Disabled button: <code>[Soon](buttonurl://disabled)</code>\n\n"
    "<b>Rich messages</b>\n"
    "Replying to a Rich Message (dividers, pull-quotes, expandable quotes) with "
    "<code>/filter keyword</code> works too."
)

# True karoge to jo plain filter me koi quote nahi hai use poora blockquote me wrap kar dega.
# Default False => jaisa user ne bheja bilkul waisa hi jayega.
WRAP_PLAIN_IN_QUOTE = False

BLANK = "\u3164"  # sirf buttons wale filter ke liye invisible text

_STYLES = ("danger", "success", "primary")

_ROSE_BTN_RE = re.compile(r"\[([^\[\]\n]+?)\]\(\s*buttonurl:(?://)?([^\s)]+)\s*\)", re.I)
_TILDE_BTN_RE = re.compile(r"\[([^\[\]\n~]+?)\s*~\s*((?:https?|tg)://[^\s\]]+)\s*\]", re.I)


# ───────────────────────── helpers ─────────────────────────

def _u16(s: str) -> int:
    """Length in UTF-16 code units (Telegram entity offsets yahi use karta hai)."""
    return len(s.encode("utf-16-le")) // 2


def _make_button(text, **kw):
    """Thin wrapper -> common.make_button (style / icon_custom_emoji_id / disabled via api_kwargs)."""
    return make_button(
        text,
        url=kw.get("url"),
        callback_data=kw.get("callback_data"),
        style=kw.get("style"),
        icon_custom_emoji_id=kw.get("icon_custom_emoji_id"),
        disabled=bool(kw.get("disabled")),
    )


def _btn_to_dict(btn):
    """InlineKeyboardButton -> storable dict. URL buttons and disabled buttons only."""
    ak = getattr(btn, "api_kwargs", None) or {}
    d = {"text": btn.text}
    if btn.url:
        d["url"] = btn.url
    elif ak.get("disabled") is not None or getattr(btn, "disabled", None) is not None:
        d["disabled"] = True
    else:
        return None
    style = getattr(btn, "style", None) or ak.get("style")
    if style:
        d["style"] = style
    icon = getattr(btn, "icon_custom_emoji_id", None) or ak.get("icon_custom_emoji_id")
    if icon:
        d["icon_custom_emoji_id"] = icon
    return d


def _extract_native_buttons(reply_msg):
    """Telegram ke native inline URL buttons (JOIN wale etc.) save karo."""
    if not reply_msg or not reply_msg.reply_markup:
        return None
    markup = reply_msg.reply_markup
    if not hasattr(markup, "inline_keyboard") or not markup.inline_keyboard:
        return None

    rows = []
    for row in markup.inline_keyboard:
        btn_row = [d for d in (_btn_to_dict(btn) for btn in row) if d]
        if btn_row:
            rows.append(btn_row)
    return rows if rows else None


def _kb_from_stored(rows):
    if not rows:
        return None
    kb_rows = []
    for row in rows:
        kb_row = []
        for b in row:
            if not b.get("url") and not b.get("disabled"):
                continue
            kb_row.append(_make_button(
                b["text"],
                url=b.get("url"),
                style=b.get("style"),
                icon_custom_emoji_id=b.get("icon_custom_emoji_id"),
                disabled=b.get("disabled"),
            ))
        if kb_row:
            kb_rows.append(kb_row)
    return InlineKeyboardMarkup(kb_rows) if kb_rows else None


def _drop_icons(rows):
    """Copy of stored button rows without custom-emoji icons (retry path)."""
    out = copy.deepcopy(rows or [])
    for row in out:
        for b in row:
            b.pop("icon_custom_emoji_id", None)
    return out


def _extract_buttons(text: str, entities: list):
    """Text me se button syntax nikalo AUR entities ke offsets sahi shift karo.

    Returns (clean_text, clean_entities(list of dict), rows(list of list of dict)).
    """
    entities = entities or []
    if not text:
        return text, entities, []

    matches = []
    for m in _ROSE_BTN_RE.finditer(text):
        matches.append(("rose", m))
    for m in _TILDE_BTN_RE.finditer(text):
        matches.append(("tilde", m))
    matches.sort(key=lambda x: x[1].start())

    rows, ranges_c = [], []   # ranges_c = removal ranges (char index)
    last_end = 0
    for kind, m in matches:
        if m.start() < last_end:
            continue
        label = m.group(1).strip()
        raw_tail = m.group(2).strip()
        same, style, icon, disabled = False, None, None, False
        if kind == "rose":
            spec = parse_button_tail(raw_tail)
            same, style, icon, disabled = spec["same"], spec["style"], spec["icon"], spec["disabled"]
            url = normalize_url(spec["url"]) if spec["url"] else ""
        else:
            url = raw_tail
        if not label or not (url or disabled):
            continue

        btn = {"text": label}
        if disabled:
            btn["disabled"] = True
        else:
            btn["url"] = url
        if style in _STYLES:
            btn["style"] = style
        if icon:
            btn["icon_custom_emoji_id"] = icon
        if same and rows:
            rows[-1].append(btn)
        else:
            rows.append([btn])

        s, e = m.start(), m.end()
        if s > last_end and text[s - 1] == "\n":   # button se pehle ka newline bhi hata do
            s -= 1
        ranges_c.append((s, e))
        last_end = e

    if not ranges_c:
        return text, entities, []

    # naya text
    parts, pos = [], 0
    for s, e in ranges_c:
        parts.append(text[pos:s])
        pos = e
    parts.append(text[pos:])
    new_text = "".join(parts).rstrip()
    new_len = _u16(new_text)

    ranges_u = [(_u16(text[:s]), _u16(text[:e])) for s, e in ranges_c]

    def mp(x):
        return x - sum(max(0, min(x, re_) - rs) for rs, re_ in ranges_u)

    new_ents = []
    for ent in entities:
        st = mp(ent["offset"])
        en = min(mp(ent["offset"] + ent["length"]), new_len)
        if en > st:
            d = dict(ent)
            d["offset"], d["length"] = st, en - st
            new_ents.append(d)

    return new_text, new_ents, rows


def _to_entities(items, bot=None):
    out = []
    for d in items or []:
        try:
            out.append(MessageEntity.de_json(d, bot))
        except Exception:
            try:
                out.append(MessageEntity(type=d["type"], offset=d["offset"], length=d["length"],
                                         url=d.get("url"), language=d.get("language"),
                                         custom_emoji_id=d.get("custom_emoji_id"),
                                         unix_time=d.get("unix_time"),
                                         date_time_format=d.get("date_time_format")))
            except Exception:
                pass
    return [e for e in out if e is not None]


def _merge_kb(*kbs):
    rows = []
    for kb in kbs:
        if kb and kb.inline_keyboard:
            rows.extend(kb.inline_keyboard)
    return InlineKeyboardMarkup(rows) if rows else None


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


# ───────────────────────── commands ─────────────────────────

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

    data = {
        "v": 2, "type": "text", "content": "", "caption": "", "entities": [],
        "buttons": _extract_native_buttons(reply), "set_by": update.effective_user.id,
    }

    def ents_of(entities):
        return [e.to_dict() for e in (entities or [])]

    rich = get_rich_message(reply)

    if rich:
        # Bot API 10.1+ Rich Message (dividers, pull-quotes, expandable quotes, ...).
        # Isme .text / .caption nahi hota - isi wajah se pehle "Unsupported message type" aata tha.
        # Poora raw object save karte hain + source message ka pointer (copy fallback ke liye).
        data.update(type="rich", content="", rich=rich,
                    src_chat=reply.chat_id, src_msg=reply.message_id)
    elif reply.text:
        # Normal text, expandable blockquote ke saath bhi: entity "expandable_blockquote" as-it-is jati hai.
        data.update(type="text", content=reply.text, entities=ents_of(reply.entities))
        lpo = getattr(reply, "link_preview_options", None)
        if lpo is not None and getattr(lpo, "is_disabled", False):
            data["no_preview"] = True
    elif reply.sticker:
        data.update(type="sticker", content=reply.sticker.file_id)
    elif reply.photo:
        data.update(type="photo", content=reply.photo[-1].file_id)
    elif reply.video:
        data.update(type="video", content=reply.video.file_id)
    elif reply.animation:
        data.update(type="animation", content=reply.animation.file_id)
    elif reply.audio:
        data.update(type="audio", content=reply.audio.file_id)
    elif reply.voice:
        data.update(type="voice", content=reply.voice.file_id)
    elif reply.video_note:
        data.update(type="video_note", content=reply.video_note.file_id)
    elif reply.document:
        data.update(type="document", content=reply.document.file_id)
    else:
        try:
            log.warning("filter: unsupported reply, message fields = %s", sorted(reply.to_dict().keys()))
        except Exception:
            pass
        await msg.reply_text("⚠️ Unsupported message type.")
        return

    if data["type"] not in ("text", "rich"):
        data["caption"] = reply.caption or ""
        data["entities"] = ents_of(reply.caption_entities)
        if getattr(reply, "has_media_spoiler", False):
            data["spoiler"] = True
        if getattr(reply, "show_caption_above_media", False):
            data["caption_above"] = True

    await dbase.filter_set(chat.id, keyword, data)

    verify = await dbase.filter_get(chat.id, keyword)
    if verify:
        await msg.reply_text(f"<blockquote>✅ Filter <b>{keyword}</b> saved with formatting and buttons.</blockquote>")
    else:
        await msg.reply_text(f"<blockquote>❌ Filter <b>{keyword}</b> could not be saved — check bot logs.</blockquote>")


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
        await msg.reply_text(f"<blockquote>🗑 Filter <b>{keyword}</b> removed.</blockquote>")
    else:
        await msg.reply_text(f"<blockquote>❓ No filter named <b>{keyword}</b> in this chat.</blockquote>")


async def list_filters_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    rows = await dbase.filter_list(chat.id)
    if not rows:
        await msg.reply_text("<blockquote>No filters set in this chat yet.</blockquote>")
        return
    lines = [f"• <code>{r['keyword']}</code>  <i>({r.get('type', 'text')})</i>" for r in rows]
    header = f"<b>🔍 Filters in this chat — {len(rows)}</b>\n\n"
    text_body = "\n".join(lines)
    await msg.reply_text(f"<blockquote>{header}{text_body}</blockquote>")


async def unfilterall_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/unfilterall & .unfilterall — asks for a red confirmation button."""
    msg, chat = update.effective_message, update.effective_chat
    if not await _is_admin(update):
        await msg.reply_text("❌ Only admins can wipe filters.")
        return

    rows = await dbase.filter_list(chat.id)
    if not rows:
        await msg.reply_text("<blockquote>There are no filters to delete.</blockquote>")
        return

    text = (
        f"<blockquote>⚠️ <b>Delete ALL {len(rows)} filter(s)?</b>\n"
        f"This action cannot be undone.</blockquote>"
    )

    yes_btn = _make_button("🗑 Yes, delete all", callback_data="unfilterall_yes", style="danger")
    no_btn = _make_button("❌ Cancel", callback_data="unfilterall_no")

    kb = InlineKeyboardMarkup([[yes_btn, no_btn]])
    await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def unfilterall_confirm_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/unfilterall confirmation callback — only admins can confirm."""
    query = update.callback_query
    if not query:
        return

    user = update.effective_user
    chat = update.effective_chat

    if chat.type != ChatType.PRIVATE:
        try:
            member = await chat.get_member(user.id)
            is_admin = member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
        except Exception:
            is_admin = False
        if not is_admin:
            await query.answer("❌ Only admins can confirm this.", show_alert=True)
            return

    data = query.data or ""

    if data == "unfilterall_no":
        await query.answer("Cancelled.")
        try:
            await query.edit_message_text(
                "<blockquote>❎ Cancelled — no filters were deleted.</blockquote>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    if data == "unfilterall_yes":
        await query.answer("Deleting…")
        n = await dbase.filter_delete_all(chat.id)
        try:
            await query.edit_message_text(
                f"<blockquote>🗑 Deleted <b>{n}</b> filter(s) from this chat.</blockquote>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            try:
                await query.message.reply_text(
                    f"<blockquote>🗑 Deleted <b>{n}</b> filter(s) from this chat.</blockquote>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass


# ───────────────────────── trigger ─────────────────────────

_CAPTION_METHODS = {
    "photo": "reply_photo",
    "video": "reply_video",
    "animation": "reply_animation",
    "audio": "reply_audio",
    "voice": "reply_voice",
    "document": "reply_document",
}
_PLAIN_METHODS = {          # caption support nahi
    "sticker": "reply_sticker",
    "video_note": "reply_video_note",
}


# ───────────────────────── rich messages (Bot API 10.1+) ─────────────────────────
# helpers common.py me hain (welcome.py bhi use karta hai)

async def _send_rich(msg, bot, row, kb):
    thread = msg.message_thread_id if getattr(msg, "is_topic_message", False) else None
    await send_rich(bot, msg.chat_id, row, kb, reply_to=msg.message_id, thread_id=thread)


async def _send_filter(msg, bot, row, allow_icons=True):
    ftype = row.get("type", "text")
    if ftype == "rich":
        native = row.get("buttons")
        await _send_rich(msg, bot, row, _kb_from_stored(native if allow_icons else _drop_icons(native)))
        return
    content = row.get("content", "")
    caption = row.get("caption", "") or ""
    modern = row.get("v") == 2

    text_part = content if ftype == "text" else caption
    entities = row.get("entities") or []
    api_kwargs = {}
    parse_mode = None
    custom_rows = []

    if modern:
        if text_part:
            text_part, entities, custom_rows = _extract_buttons(text_part, entities)
        if WRAP_PLAIN_IN_QUOTE and text_part and not any(
            e.get("type") in ("blockquote", "expandable_blockquote") for e in entities
        ):
            entities = list(entities) + [{"type": "blockquote", "offset": 0, "length": _u16(text_part)}]
        ents = _to_entities(entities, bot)
    else:
        # ---- legacy (HTML) filters ----
        ents = None
        parse_mode = ParseMode.HTML
        if text_part:
            text_part, legacy_kb = parse_buttons(text_part)
        else:
            legacy_kb = None
        if legacy_kb and legacy_kb.inline_keyboard:
            custom_rows = [[d for d in (_btn_to_dict(b) for b in r) if d] for r in legacy_kb.inline_keyboard]
        if text_part and "<blockquote" not in text_part:
            text_part = f"<blockquote>{text_part}</blockquote>"

    native_rows = row.get("buttons")
    if not allow_icons:
        custom_rows, native_rows = _drop_icons(custom_rows), _drop_icons(native_rows)
    custom_kb = _kb_from_stored(custom_rows)
    native_kb = _kb_from_stored(native_rows)
    final_kb = _merge_kb(custom_kb, native_kb)

    base = {"reply_markup": final_kb, "reply_to_message_id": msg.message_id}

    if ftype == "text":
        if not text_part:
            text_part = BLANK
        if row.get("no_preview"):
            api_kwargs["link_preview_options"] = {"is_disabled": True}
        kw = dict(base, parse_mode=parse_mode)
        if ents:
            kw["entities"] = ents
        if api_kwargs:
            kw["api_kwargs"] = api_kwargs
        try:
            await msg.reply_text(text_part, **kw)
        except BadRequest as e:
            # Kuch clients/servers U+3164 ko empty maante hain (buttons-only filter) -> braille blank try karo.
            if text_part == BLANK and "empty" in str(e).lower():
                await msg.reply_text("\u2800", **kw)
            else:
                raise
        return

    if ftype in _PLAIN_METHODS:
        await getattr(msg, _PLAIN_METHODS[ftype])(content, **base)
        return

    method = _CAPTION_METHODS.get(ftype)
    if not method:
        return
    if row.get("spoiler") and ftype in ("photo", "video", "animation"):
        api_kwargs["has_spoiler"] = True
    if row.get("caption_above") and text_part and ftype in ("photo", "video", "animation"):
        api_kwargs["show_caption_above_media"] = True
    kw = dict(base, caption=text_part or None, parse_mode=parse_mode)
    if ents and text_part:
        kw["caption_entities"] = ents
    if api_kwargs:
        kw["api_kwargs"] = api_kwargs
    await getattr(msg, method)(content, **kw)


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

        try:
            try:
                await _send_filter(msg, ctx.bot, row)
            except BadRequest as e:
                # Custom-emoji icon on a button is rejected unless the bot/owner is allowed to use it
                # -> same filter ko icons ke bina dobara bhejo, taaki filter silent fail na ho.
                if "emoji" not in str(e).lower():
                    raise
                log.warning("filter icons rejected (%s) - retrying without icons", e)
                await _send_filter(msg, ctx.bot, row, allow_icons=False)
        except Exception as e:
            log.error("filter send failed [%s/%s]: %s", chat.id, word, e)
            try:
                await msg.reply_text(f"❌ Error sending filter: <code>{e}</code>", reply_to_message_id=msg.message_id)
            except Exception:
                pass

        break


def register(app: Application):
    app.add_handler(MessageHandler(
        tg_filters.Regex(r"^[./]filter(?:\s+(.+))?$") & tg_filters.ChatType.GROUPS,
        filter_cmd
    ), group=0)

    app.add_handler(MessageHandler(
        tg_filters.Regex(r"^[./]unfilterall$") & tg_filters.ChatType.GROUPS,
        unfilterall_cmd
    ), group=0)

    app.add_handler(MessageHandler(
        tg_filters.Regex(r"^[./]unfilter(?:\s+(.+))?$") & tg_filters.ChatType.GROUPS,
        unfilter_cmd
    ), group=0)

    app.add_handler(MessageHandler(
        tg_filters.Regex(r"^[./]filters$") & tg_filters.ChatType.GROUPS,
        list_filters_cmd
    ), group=0)

    app.add_handler(CallbackQueryHandler(
        unfilterall_confirm_cb,
        pattern=r"^unfilterall_(yes|no)$"
    ), group=0)

    app.add_handler(MessageHandler(
        (tg_filters.TEXT | tg_filters.CAPTION) & tg_filters.ChatType.GROUPS,
        trigger_filter
    ), group=-2)
    
