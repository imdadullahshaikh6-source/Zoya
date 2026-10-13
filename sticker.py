"""Sticker plugin:
.q / .qr  — render the replied-to message as a Telegram-style quote sticker
.kang     — steal a replied sticker/photo/GIF/video into the user's own auto-growing pack
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from collections import OrderedDict

import httpx
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont
from telegram import (
    InlineKeyboardMarkup,
    InputSticker,
    MessageEntity,
)
from telegram.error import TelegramError
from telegram.ext import MessageHandler, filters

import database as dbase
from common import (
    T, dual_command, esc, say, B,
    QUOTE_API, QUOTE_API_FALLBACKS, QUOTE_BG,
    QUOTE_TIMEOUT, QUOTE_TOTAL_TIMEOUT,
)

log = logging.getLogger("sticker")

HELP_TXT = (
    "<b>✦ stickers</b>\n\n"
    "/q (or .q) — reply to any message to turn it into a quote sticker\n"
    "/qr (or .qr, /q r) — nested quote: also shows the message that the quoted "
    "message was replying to, and is sent as a reply to the original message\n"
    "/kang (or .kang) [emoji] [pack#] — reply to a sticker, photo, GIF or video "
    "to add it to your own sticker pack."
)
COMMANDS = [("q", "Quote a message as a sticker"), ("kang", "Steal a sticker into your pack")]

MAX_SIDE = 512
EMOJI_RE = re.compile(r"^[\U0001F000-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF]+$")

EMOJI_EXTRACT_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u27BF"
    "\u2B00-\u2BFF"
    "\u2190-\u21FF"
    "\u2300-\u23FF"
    "\u25A0-\u25FF"
    "]+"
)

_FALLBACKS = [u.strip() for u in (QUOTE_API_FALLBACKS or "").split(",") if u.strip()]
TOTAL_TIMEOUT = QUOTE_TOTAL_TIMEOUT

COOLDOWN = 8.0
_AVATAR_TTL = 600

_last_quote: dict[int, float] = {}
_avatar_cache: dict[int, tuple[float, str | None]] = {}

_KEEP_ENTITIES = {
    "bold", "italic", "underline", "strikethrough", "spoiler",
    "code", "pre", "blockquote", "text_link", "text_mention",
}
_ENTITY_TYPE_STR = {
    MessageEntity.BOLD: "bold",
    MessageEntity.ITALIC: "italic",
    MessageEntity.UNDERLINE: "underline",
    MessageEntity.STRIKETHROUGH: "strikethrough",
    MessageEntity.SPOILER: "spoiler",
    MessageEntity.CODE: "code",
    MessageEntity.PRE: "pre",
    MessageEntity.BLOCKQUOTE: "blockquote",
    MessageEntity.EXPANDABLE_BLOCKQUOTE: "blockquote",
    MessageEntity.TEXT_LINK: "text_link",
    MessageEntity.TEXT_MENTION: "text_mention",
}


# ───────── Pillow helpers ─────────
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
FONT_REGULAR = os.path.join(FONT_DIR, "DejaVuSans.ttf")
FONT_BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")

BUBBLE_BG = (43, 43, 63, 255)
PEER_COLORS = [
    (225, 112, 118), (250, 167, 116), (166, 149, 231),
    (123, 200, 98), (110, 201, 203), (101, 170, 221), (238, 122, 174),
]


def _load_font(bold: bool, size: int):
    paths = [FONT_BOLD if bold else FONT_REGULAR]
    if bold:
        paths.append(FONT_REGULAR)
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _cmap_of(path):
    try:
        return set(TTFont(path, fontNumber=0, lazy=True).getBestCmap())
    except Exception:
        return set()


_CMAP = {False: _cmap_of(FONT_REGULAR), True: _cmap_of(FONT_BOLD)}
if not _CMAP[True]:
    _CMAP[True] = _CMAP[False]
_DROP_CATEGORIES = {"Cf", "Cc", "Co", "Cs", "Mn", "Me"}


def _renderable(bold: bool, text: str, fallback: str = "") -> str:
    import unicodedata
    cmap = _CMAP[bold]
    out = []
    for ch in unicodedata.normalize("NFKD", text or ""):
        if unicodedata.category(ch) in _DROP_CATEGORIES:
            continue
        if ch in (" ", "\n", "\t") or ord(ch) in cmap:
            out.append(ch)
    cleaned = re.sub(r"\s+", " ", "".join(out)).strip()
    return cleaned or fallback


def _peer_color(user_id: int):
    return PEER_COLORS[abs(user_id) % len(PEER_COLORS)]


def _fit_512(img: Image.Image) -> Image.Image:
    w, h = img.size
    scale = MAX_SIDE / max(w, h)
    return img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def _square_512(img: Image.Image) -> Image.Image:
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    return img.resize((MAX_SIDE, MAX_SIDE), Image.LANCZOS)


def _wrap(draw, text, font, max_w):
    words = text.split() or [""]
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines[:10] or [""]


async def _fetch_avatar_bytes(ctx, user_id: int) -> bytes | None:
    try:
        async def _fetch():
            photos = await ctx.bot.get_user_profile_photos(user_id, limit=1)
            if not photos.total_count:
                return None
            file_id = photos.photos[0][-1].file_id
            tgfile = await ctx.bot.get_file(file_id)
            return bytes(await tgfile.download_as_bytearray())
        return await asyncio.wait_for(_fetch(), timeout=6.0)
    except (asyncio.TimeoutError, TelegramError, OSError) as e:
        log.debug("avatar fetch failed for %s: %s", user_id, e)
        return None


async def _avatar_url(ctx, user_id: int, force_refresh: bool = False) -> str | None:
    now = time.time()
    if not force_refresh:
        cached = _avatar_cache.get(user_id)
        if cached is not None and cached[0] > now:
            return cached[1]

    url = None
    try:
        bot_token = os.environ.get("BOT_TOKEN", "").strip()
        if bot_token:
            photos = await ctx.bot.get_user_profile_photos(user_id, limit=1)
            if photos.total_count:
                file_id = photos.photos[0][-1].file_id
                tgfile = await ctx.bot.get_file(file_id)
                if tgfile.file_path:
                    url = f"https://api.telegram.org/file/bot{bot_token}/{tgfile.file_path}"
    except (TelegramError, OSError) as e:
        log.debug("avatar url failed for %s: %s", user_id, e)

    _avatar_cache[user_id] = (now + _AVATAR_TTL, url)
    return url


# ───────── entity extraction ─────────

def _extract_entities(msg) -> list[dict]:
    raw = list(msg.entities or []) + list(msg.caption_entities or [])
    out = []
    for e in raw:
        kind = _ENTITY_TYPE_STR.get(e.type)
        if not kind or kind not in _KEEP_ENTITIES:
            continue
        if not e.length or e.length <= 0:
            continue
        item = {"type": kind, "offset": int(e.offset), "length": int(e.length)}
        if kind == "text_link" and getattr(e, "url", None):
            item["url"] = e.url
        if kind == "pre" and getattr(e, "language", None):
            item["language"] = e.language
        if kind == "text_mention" and getattr(e, "user", None):
            item["user"] = {"id": e.user.id, "name": e.user.full_name}
        out.append(item)
    return out


def _text_of(msg) -> str:
    return (getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip()


def _from_block(user) -> dict:
    if user is None:
        return {"id": 0, "first_name": "User", "name": "User"}
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(p for p in parts if p).strip() or "User"
    out = {"id": int(user.id or 0), "first_name": user.first_name or name, "name": name}
    if getattr(user, "username", None):
        out["username"] = user.username
    return out


def _build_reply_block(reply) -> dict | None:
    if reply is None:
        return None
    text = _text_of(reply)
    if not text:
        return None
    frm = _from_block(getattr(reply, "from_user", None))
    return {
        "name": frm.get("name") or "User",
        "text": text,
        "entities": _extract_entities(reply),
        "chatId": int(getattr(reply, "chat_id", 0) or 0),
        "from": frm,
    }


def _build_message(msg, avatar: str | None = None, reply_override: dict | None = None) -> dict:
    block = {
        "entities": _extract_entities(msg),
        "avatar": True,
        "from": _from_block(getattr(msg, "from_user", None)),
        "text": _text_of(msg),
    }
    if avatar:
        block["from"]["photo"] = {"url": avatar}
    reply_block = _build_reply_block(getattr(msg, "reply_to_message", None))
    if reply_block:
        block["replyMessage"] = reply_block
    if reply_override:
        block["replyMessage"] = reply_override
    return block


def _build_payload(msg, avatar: str | None = None, reply_override: dict | None = None) -> dict:
    return {
        "type": "quote",
        "format": "webp",
        "backgroundColor": QUOTE_BG,
        "width": 512,
        "height": 512,
        "scale": 1,
        "emojiBrand": "apple",
        "messages": [_build_message(msg, avatar, reply_override)],
    }


# ───────── API path ─────────

async def _try_one_api(client: httpx.AsyncClient, url: str, payload: dict) -> bytes | None:
    try:
        r = await client.post(url, json=payload)
        if r.status_code != 200:
            log.warning("⚠️ %s returned %s", url, r.status_code)
            return None
        data = r.content
        if not data or len(data) < 100:
            log.warning("⚠️ %s returned empty body", url)
            return None

        try:
            j = json.loads(data)
        except Exception:
            j = None

        if isinstance(j, dict):
            img_b64 = None
            if isinstance(j.get("result"), dict):
                img_b64 = j["result"].get("image")
            if not img_b64:
                img_b64 = j.get("image") or j.get("data")
            if isinstance(img_b64, str):
                try:
                    decoded = base64.b64decode(img_b64)
                    if len(decoded) > 100:
                        log.info("✅ quote built via %s (JSON→base64, %d bytes)", url, len(decoded))
                        return decoded
                except Exception as e:
                    log.warning("⚠️ %s base64 decode failed: %s", url, e)
                    return None

        log.info("✅ quote built via %s (%d bytes)", url, len(data))
        return data
    except Exception as e:
        log.warning("⚠️ %s failed: %s", url, e)
        return None


async def _build_quote_via_api(ctx, src_msg, sender, reply_override: dict | None = None) -> bytes | None:
    text = _text_of(src_msg)
    if not text:
        return None

    avatar_url = await _avatar_url(ctx, sender.id)
    log.info("[sticker] avatar url: %s", "yes" if avatar_url else "none")

    payload = _build_payload(src_msg, avatar_url, reply_override)
    msg0 = payload["messages"][0]
    if "replyMessage" in msg0:
        log.info("[sticker] nested reply: %r", msg0["replyMessage"].get("text", "")[:50])
    else:
        log.info("[sticker] no nested reply")

    urls = [QUOTE_API] + _FALLBACKS
    timeout = httpx.Timeout(QUOTE_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        tasks = [_try_one_api(client, url, payload) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, bytes) and len(r) > 100:
                return r
    return None


# ───────── Pillow fallback ─────────

async def _build_quote_via_pillow(ctx, src_msg, sender) -> bytes:
    log.info("[sticker] rendering locally with Pillow")
    W = 512
    outer_pad = 12
    avatar_size = 92
    gap = 14
    inner_pad = 22

    font_name = _load_font(True, 30)
    font_text = _load_font(False, 36)

    raw_text = _text_of(src_msg) or "[media]"
    text = _renderable(False, raw_text, "[unsupported characters]")
    sender_name = re.sub(r"\s+", " ", (sender.full_name or "Unknown")).strip()

    bubble_x = outer_pad + avatar_size + gap
    bubble_w = W - bubble_x - outer_pad
    text_w = bubble_w - inner_pad * 2

    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    name_lines = _wrap(probe, sender_name, font_name, text_w)
    name_line_h = font_name.getbbox("Ag")[3] + 6
    text_lines = _wrap(probe, text, font_text, text_w)
    text_line_h = font_text.getbbox("Ag")[3] + 12

    gap_name_text = 12
    body_h = name_line_h * len(name_lines) + gap_name_text + text_line_h * len(text_lines)
    bubble_h = body_h + inner_pad * 2
    H = max(avatar_size, bubble_h) + outer_pad * 2

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    by = (H - bubble_h) // 2
    draw.rounded_rectangle(
        (bubble_x, by, bubble_x + bubble_w, by + bubble_h),
        radius=26, fill=BUBBLE_BG,
    )

    ty = by + inner_pad
    name_color = _peer_color(sender.id) if sender else (255, 255, 255)
    for ln in name_lines:
        draw.text((bubble_x + inner_pad, ty), ln, font=font_name, fill=name_color)
        ty += name_line_h
    ty += gap_name_text
    for ln in text_lines:
        draw.text((bubble_x + inner_pad, ty), ln, font=font_text, fill=(255, 255, 255))
        ty += text_line_h

    photo = await _fetch_avatar_bytes(ctx, sender.id)
    avatar = Image.new("RGBA", (avatar_size, avatar_size), (0, 0, 0, 0))
    if photo:
        try:
            av = Image.open(io.BytesIO(photo)).convert("RGBA")
            w, h = av.size
            side = min(w, h)
            av = av.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
            av = av.resize((avatar_size, avatar_size), Image.LANCZOS)
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, avatar_size, avatar_size), fill=255)
            av.putalpha(mask)
            avatar = av
        except Exception:
            photo = None
    if not photo:
        d = ImageDraw.Draw(avatar)
        d.ellipse((0, 0, avatar_size, avatar_size), fill=_peer_color(sender.id))
        f = _load_font(True, avatar_size // 2)
        words = re.findall(r"[A-Za-z]+", sender_name)
        initials = (words[0][0] + (words[1][0] if len(words) > 1 else "")).upper() if words else "?"
        bbox = d.textbbox((0, 0), initials, font=f)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(((avatar_size - tw) / 2 - bbox[0], (avatar_size - th) / 2 - bbox[1]),
               initials, font=f, fill=(255, 255, 255))
    img.paste(avatar, (outer_pad, (H - avatar_size) // 2), avatar)

    img = _fit_512(img)
    out = io.BytesIO()
    img.save(out, "WEBP")
    return out.getvalue()


async def _build_quote_sticker(ctx, src_msg, sender, reply_override: dict | None = None) -> bytes:
    try:
        data = await _build_quote_via_api(ctx, src_msg, sender, reply_override)
        if data:
            return data
    except Exception as e:
        log.warning("[sticker] API path raised: %s", e)
    log.warning("[sticker] all APIs failed — using local Pillow fallback")
    return await _build_quote_via_pillow(ctx, src_msg, sender)


# ───────── nested-quote parent cache ─────────
_PARENT_CACHE_MAX = 20000
_parent_cache: "OrderedDict[tuple[int, int], dict]" = OrderedDict()


def _media_label(m) -> str | None:
    if getattr(m, "photo", None):
        return "📷 Photo"
    if getattr(m, "sticker", None):
        return f"{m.sticker.emoji or ''} Sticker".strip()
    if getattr(m, "animation", None):
        return "GIF"
    if getattr(m, "video", None):
        return "🎬 Video"
    if getattr(m, "video_note", None):
        return "🎬 Video message"
    if getattr(m, "voice", None):
        return "🎤 Voice message"
    if getattr(m, "audio", None):
        return "🎵 Audio"
    if getattr(m, "document", None):
        return "📄 " + (m.document.file_name or "File")
    if getattr(m, "poll", None):
        return "📊 Poll"
    return None


def _snapshot_parent(parent) -> dict | None:
    if parent is None:
        return None
    text = _text_of(parent)
    entities = _extract_entities(parent) if text else []
    if not text:
        text = _media_label(parent) or ""
    if not text:
        return None
    user = getattr(parent, "from_user", None)
    chat_obj = getattr(parent, "sender_chat", None)
    if user is None and chat_obj is not None:
        title = chat_obj.title or "Channel"
        frm = {"id": int(chat_obj.id), "first_name": title, "name": title}
    else:
        frm = _from_block(user)
    return {
        "name": frm.get("name") or "User",
        "text": text,
        "entities": entities,
        "chatId": int(getattr(parent, "chat_id", 0) or 0),
        "from": frm,
    }


def _remember_parent(chat_id: int, msg_id: int, block: dict) -> None:
    key = (chat_id, msg_id)
    _parent_cache[key] = block
    _parent_cache.move_to_end(key)
    while len(_parent_cache) > _PARENT_CACHE_MAX:
        _parent_cache.popitem(last=False)


async def _cache_reply_watcher(update, ctx):
    m = update.effective_message
    chat = update.effective_chat
    if not m or not chat or not m.reply_to_message:
        return
    if not _text_of(m):
        return
    block = _snapshot_parent(m.reply_to_message)
    if block:
        _remember_parent(chat.id, m.message_id, block)


def _find_parent_block(chat_id: int, src) -> dict | None:
    live = getattr(src, "reply_to_message", None)
    if live is not None:
        block = _snapshot_parent(live)
        if block:
            return block
    return _parent_cache.get((chat_id, src.message_id))


# ───────── parent-message helper ─────────

async def _ensure_parent(ctx, chat_id: int, src):
    if getattr(src, "reply_to_message", None):
        return src.reply_to_message

    parent_id = None
    ext = getattr(src, "external_reply", None)
    if ext is not None:
        parent_id = getattr(ext, "message_id", None)
    if not parent_id:
        parent_id = getattr(src, "reply_to_message_id", None)
    if not parent_id:
        rt = getattr(src, "reply_to_message", None)
        if rt is not None:
            parent_id = getattr(rt, "message_id", None)

    log.info("[sticker] parent_id detected: %r", parent_id)
    if not parent_id:
        return None

    try:
        fetched = await ctx.bot.get_messages(chat_id=chat_id, message_ids=parent_id)
        if isinstance(fetched, list):
            fetched = fetched[0] if fetched else None
        return fetched
    except Exception as e:
        log.warning("[sticker] couldn't fetch parent %s: %s", parent_id, e)
        return None


# ───────── quote command handler ─────────

def _cooldown_left(uid: int) -> float:
    return max(0.0, _last_quote.get(uid, 0.0) + COOLDOWN - time.time())


async def _quote_and_send(update, ctx, as_reply: bool, nested: bool = False):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    src = msg.reply_to_message

    if not src:
        await say(ctx, chat.id, T("reply to a message with /q to turn it into a sticker."), reply_to=msg.message_id)
        return
    if not _text_of(src):
        await say(ctx, chat.id, T("only text messages can be quoted."), reply_to=msg.message_id)
        return

    log.info("[sticker] src.reply_to_message present: %s", bool(getattr(src, "reply_to_message", None)))
    parent = await _ensure_parent(ctx, chat.id, src)
    if parent and parent is not getattr(src, "reply_to_message", None):
        try:
            src.reply_to_message = parent
        except Exception:
            pass
    if parent:
        log.info("[sticker] parent text: %r", _text_of(parent)[:60])
    else:
        log.info("[sticker] no parent message found")

    reply_override = None
    nested_missing = False
    if nested:
        reply_override = _find_parent_block(chat.id, src)
        nested_missing = reply_override is None
        log.info("[sticker] nested requested, parent block found: %s", not nested_missing)

    left = _cooldown_left(user.id)
    if left > 0:
        await say(ctx, chat.id, T(f"⏳ please wait {int(left) + 1}s before the next quote."), reply_to=msg.message_id)
        return
    _last_quote[user.id] = time.time()

    sender = src.from_user or user
    status = await say(ctx, chat.id, T("⏳ generating quote..."), reply_to=msg.message_id)

    async def _auto_delete_status():
        await asyncio.sleep(20)
        try:
            await status.delete()
        except Exception:
            pass

    auto_task = asyncio.create_task(_auto_delete_status())

    try:
        sticker_bytes = await asyncio.wait_for(
            _build_quote_sticker(ctx, src, sender, reply_override), timeout=TOTAL_TIMEOUT
        )
    except asyncio.TimeoutError:
        log.error("[sticker] total timeout reached")
        auto_task.cancel()
        try:
            await status.edit_text(T("❌ quote generation timed out. try again."))
        except Exception:
            pass
        return
    except Exception as e:
        log.error("[sticker] failed: %s", e, exc_info=True)
        auto_task.cancel()
        try:
            await status.edit_text(T("❌ couldn't build that sticker:") + f" {esc(e)}")
        except Exception:
            pass
        return

    auto_task.cancel()
    try:
        await status.delete()
    except Exception:
        pass

    buf = io.BytesIO(sticker_bytes)
    buf.name = "quote.webp"

    sent = False
    if as_reply:
        try:
            await ctx.bot.send_sticker(
                chat.id, buf,
                reply_to_message_id=src.message_id,
                allow_sending_without_reply=True,
            )
            sent = True
            log.info("[sticker] quote sent as reply to %s", src.message_id)
        except Exception as e:
            log.warning("[sticker] reply-send failed, retrying without reply: %s", e)
            buf.seek(0)

    if not sent:
        try:
            await ctx.bot.send_sticker(chat.id, buf)
            log.info("[sticker] quote sent to chat %s", chat.id)
        except Exception as e:
            log.error("[sticker] send failed: %s", e)
            await say(ctx, chat.id, T("❌ couldn't send that sticker:") + f" {esc(e)}", reply_to=msg.message_id)

    if nested and nested_missing:
        try:
            note = await say(ctx, chat.id, T(
                "ℹ️ no parent message found — that message isn't a reply, or it was "
                "sent before I saw it. Made a normal quote."), reply_to=msg.message_id)

            async def _drop(n=note):
                await asyncio.sleep(8)
                try:
                    await n.delete()
                except Exception:
                    pass
            asyncio.create_task(_drop())
        except Exception:
            pass


async def q_cmd(update, ctx):
    args = [a.lower() for a in (ctx.args or [])]
    as_reply = bool(args) and args[0] in ("r", "reply", "re")
    await _quote_and_send(update, ctx, as_reply=as_reply, nested=as_reply)


async def qr_cmd(update, ctx):
    await _quote_and_send(update, ctx, as_reply=True, nested=True)


# ───────── .kang helpers ─────────

async def _photo_to_sticker_file(ctx, photo):
    tgfile = await ctx.bot.get_file(photo.file_id)
    raw = await tgfile.download_as_bytearray()
    im = Image.open(io.BytesIO(bytes(raw))).convert("RGBA")
    im = _square_512(im)
    buf = io.BytesIO()
    buf.name = "kang.webp"
    im.save(buf, "WEBP", quality=90)
    buf.seek(0)
    return buf


async def _download_tg_media_to_temp(ctx, file_id: str, suffix: str) -> str:
    """Download any Telegram file to a temp path. Caller is responsible for cleanup."""
    tgfile = await ctx.bot.get_file(file_id)
    raw = await tgfile.download_as_bytearray()
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    with open(path, "wb") as f:
        f.write(bytes(raw))
    return path


def _ffmpeg_sync(cmd: list) -> subprocess.CompletedProcess:
    """Blocking ffmpeg call — runs in a thread so uvloop/custom loops don't break."""
    return subprocess.run(cmd, capture_output=True, timeout=90)


async def _convert_to_video_sticker(path: str) -> io.BytesIO:
    """Convert any GIF / video to a 512×512 VP9 WebM with alpha channel — required
    for Telegram *video* stickers (Bot API: ≤3 s, ≤512 KB, one video stream)."""
    out_path = path.rsplit(".", 1)[0] + "_vsticker.webm"
    vf = (
        "scale=512:512:force_original_aspect_ratio=decrease,"
        "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
        "fps=30,format=yuva420p"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", path,
        "-t", "3",
        "-vf", vf,
        "-c:v", "libvpx-vp9",
        "-b:v", "450k",
        "-crf", "34",
        "-preset", "veryfast",
        "-an",
        "-pix_fmt", "yuva420p",
        out_path,
    ]

    try:
        proc = await asyncio.to_thread(_ffmpeg_sync, cmd)
    except Exception as e:
        raise RuntimeError(f"ffmpeg exec failed: {e}")

    if proc.returncode != 0 or not os.path.exists(out_path):
        err = (proc.stderr or b"").decode(errors="ignore")[:200] or "unknown"
        raise RuntimeError(f"ffmpeg failed: {err}")

    with open(out_path, "rb") as f:
        data = f.read()
    try:
        os.remove(out_path)
    except OSError:
        pass
    buf = io.BytesIO(data)
    buf.name = "kang.webm"
    buf.seek(0)
    return buf


def _is_videoish_document(doc) -> bool:
    if doc is None or not getattr(doc, "mime_type", None):
        return False
    mt = doc.mime_type.lower()
    return mt.startswith("video/") or mt in ("image/gif", "application/x-tgsticker")


# ───────── .kang ─────────

async def kang_cmd(update, ctx):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    src = msg.reply_to_message
    if not src:
        await say(ctx, chat.id,
                  T("reply to a sticker, photo, GIF or video with /kang to steal it into your pack."),
                  reply_to=msg.message_id)
        return

    raw_args = list(ctx.args or [])
    pack_num_arg: int | None = None
    extracted_emojis: list[str] = []
    for a in raw_args:
        if a.isdigit() and pack_num_arg is None:
            pack_num_arg = int(a)
        else:
            extracted_emojis.extend(EMOJI_EXTRACT_RE.findall(a))
    user_emoji: str | None = "".join(extracted_emojis) or None

    file_arg = None
    fmt: str | None = None
    tmp_paths: list[str] = []

    try:
        # 1. Sticker passthrough
        if src.sticker:
            s = src.sticker
            file_arg = s.file_id
            if s.is_video:
                fmt = "video"
            elif s.is_animated:
                fmt = "animated"
            else:
                fmt = "static"
            user_emoji = user_emoji or s.emoji or "🤔"

        # 2. Photo
        elif src.photo:
            file_arg = await _photo_to_sticker_file(ctx, src.photo[-1])
            fmt = "static"
            user_emoji = user_emoji or "🤔"

        # 3. GIF / animation / video / video-document / .tgs
        elif (src.animation or src.video or _is_videoish_document(src.document)):
            media = src.animation or src.video or src.document
            fname = getattr(media, "file_name", None) or ""
            suffix = os.path.splitext(fname)[1] or (
                ".tgs" if getattr(media, "mime_type", "") == "application/x-tgsticker"
                else ".gif" if getattr(media, "mime_type", "") == "image/gif"
                else ".mp4"
            )
            src_path = await _download_tg_media_to_temp(ctx, media.file_id, suffix)
            tmp_paths.append(src_path)
            file_arg = await _convert_to_video_sticker(src_path)
            fmt = "video"
            user_emoji = user_emoji or "🎬"

        else:
            await say(ctx, chat.id,
                      T("reply to a sticker, photo, GIF or video with /kang to steal it into your pack."),
                      reply_to=msg.message_id)
            return
    except Exception as e:
        log.error("[kang] prepare failed: %s", e, exc_info=True)
        await say(ctx, chat.id, T("couldn't read that media:") + f" {esc(e)}",
                  reply_to=msg.message_id)
        return
    finally:
        for p in tmp_paths:
            try:
                os.remove(p)
            except OSError:
                pass

    prefix = {"animated": "anim", "video": "vid", "static": "a"}[fmt]
    emoji = user_emoji or "🤔"

    me = ctx.application.bot_data["me"]
    base = re.sub(r"[^a-zA-Z0-9_]", "", (user.username or f"u{user.id}")).lower() or f"u{user.id}"
    sticker_obj = InputSticker(sticker=file_arg, emoji_list=[emoji], format=fmt)

    async def _create(part: int):
        name = f"{prefix}{part}_{base}_by_{me.username}"
        title = f"{user.first_name or 'My'}'s Pack" + (f" {part}" if part > 1 else "")
        await ctx.bot.create_new_sticker_set(user.id, name, title[:64], stickers=[sticker_obj])
        return name

    async def _refresh_thumbnail(pack_name: str):
        try:
            await ctx.bot.set_sticker_set_thumbnail(
                user.id, pack_name, thumbnail=sticker_obj.sticker
            )
            log.info("[kang] pack refreshed via thumbnail: %s", pack_name)
        except Exception as e:
            log.debug("[kang] thumbnail refresh failed (%s): %s", pack_name, e)

    rec = await dbase.kang_get(user.id)
    same_format = bool(
        rec and rec.get("name") and
        re.match(rf"^{re.escape(prefix)}\d+_", rec["name"])
    )

    try:
        if pack_num_arg is not None:
            target = f"{prefix}{pack_num_arg}_{base}_by_{me.username}"
            try:
                await ctx.bot.add_sticker_to_set(user.id, target, sticker=sticker_obj)
                name, count = target, (rec.get("count", 0) + 1 if (rec and rec.get("name") == target) else 1)
            except TelegramError as e:
                s = str(e).lower()
                if "invalid" in s or "not found" in s:
                    name = await _create(pack_num_arg)
                    count = 1
                else:
                    raise
            await dbase.kang_set(user.id, name, count, part=pack_num_arg)
            await _refresh_thumbnail(name)

        elif same_format:
            try:
                await ctx.bot.add_sticker_to_set(user.id, rec["name"], sticker=sticker_obj)
                name, count = rec["name"], rec.get("count", 0) + 1
                await dbase.kang_set(user.id, name, count, part=rec.get("part", 1))
                await _refresh_thumbnail(name)
            except TelegramError as e:
                s = str(e).lower()
                if "invalid" in s or "too much" in s or "too many" in s or "full" in s:
                    part = rec.get("part", 1) + 1
                    name = await _create(part)
                    count = 1
                    await dbase.kang_set(user.id, name, count, part=part)
                    await _refresh_thumbnail(name)
                else:
                    raise

        else:
            part = pack_num_arg or 1
            name = await _create(part)
            count = 1
            await dbase.kang_set(user.id, name, count, part=part)
            await _refresh_thumbnail(name)

    except TelegramError as e:
        await say(ctx, chat.id, T("kang failed:") + f" {esc(e)}", reply_to=msg.message_id)
        return

    link = f"https://t.me/addstickers/{name}"
    kb = InlineKeyboardMarkup([[
        B("📦 View Pack", url=link, style="primary")
    ]])

    try:
        await ctx.bot.send_message(
            chat.id,
            T("✅ added to your pack ({c} stickers so far).", c=count),
            reply_to_message_id=msg.message_id,
            allow_sending_without_reply=True,
            reply_markup=kb,
        )
    except Exception:
        await ctx.bot.send_message(
            chat.id,
            T("✅ added to your pack ({c} stickers so far).\n{l}", c=count, l=esc(link)),
            reply_markup=kb,
        )


def register(app):
    dual_command(app, "q", q_cmd)
    dual_command(app, "qr", qr_cmd)
    dual_command(app, "kang", kang_cmd)
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.REPLY & (filters.TEXT | filters.CAPTION),
            _cache_reply_watcher,
            block=False,
        ),
        group=-100,
                     )
      
