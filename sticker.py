"""Sticker plugin:
.q / .qr  — quote sticker
.kang     — steal media into your pack
.kangs    — list / delete your sticker packs
.unkang   — remove a sticker from your pack
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import time
from collections import OrderedDict

import httpx
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputSticker,
    MessageEntity,
)
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, MessageHandler, filters

import database as dbase
from common import (
    T, dual_command, esc, say, q, B,
    QUOTE_API, QUOTE_API_FALLBACKS, QUOTE_BG,
    QUOTE_TIMEOUT, QUOTE_TOTAL_TIMEOUT,
)

log = logging.getLogger("sticker")

HELP_TXT = (
    "<b>✦ stickers</b>\n\n"
    "/q (or .q) — quote a message as a sticker\n"
    "/qr (or .qr, /q r) — nested quote\n"
    "/kang (or .kang) [emoji] — add replied sticker/photo to your pack\n"
    "/kangs (or .kangs) — list / delete your sticker packs\n"
    "/unkang (or .unkang) — remove a sticker from your pack"
)
COMMANDS = [
    ("q", "Quote a message as a sticker"),
    ("kang", "Steal a sticker into your pack"),
    ("kangs", "List your sticker packs"),
    ("unkang", "Remove a sticker from your pack"),
]

MAX_SIDE = 512
EMOJI_RE = re.compile(r"^[\U0001F000-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF]+$")

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

    parent = await _ensure_parent(ctx, chat.id, src)
    if parent and parent is not getattr(src, "reply_to_message", None):
        try:
            src.reply_to_message = parent
        except Exception:
            pass

    reply_override = None
    nested_missing = False
    if nested:
        reply_override = _find_parent_block(chat.id, src)
        nested_missing = reply_override is None

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
        auto_task.cancel()
        try:
            await status.edit_text(T("❌ quote generation timed out. try again."))
        except Exception:
            pass
        return
    except Exception as e:
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
        except Exception as e:
            log.warning("[sticker] reply-send failed, retrying without reply: %s", e)
            buf.seek(0)

    if not sent:
        try:
            await ctx.bot.send_sticker(chat.id, buf)
        except Exception as e:
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


# ───────── .kang ─────────

async def kang_cmd(update, ctx):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    src = msg.reply_to_message
    if not src or not (src.sticker or src.photo):
        await say(ctx, chat.id, T("reply to a sticker or a photo with /kang to steal it into your pack."), reply_to=msg.message_id)
        return

    emoji = ctx.args[0] if ctx.args and EMOJI_RE.match(ctx.args[0]) else None

    if src.sticker:
        s = src.sticker
        emoji = emoji or s.emoji or "🤔"
        file_arg = s.file_id
        if s.is_video:
            fmt = "video"
        elif s.is_animated:
            fmt = "animated"
        else:
            fmt = "static"
    else:
        try:
            file_arg = await _photo_to_sticker_file(ctx, src.photo[-1])
        except Exception as e:
            await say(ctx, chat.id, T("couldn't read that image:") + f" {esc(e)}", reply_to=msg.message_id)
            return
        fmt = "static"
        emoji = emoji or "🤔"

    me = ctx.application.bot_data["me"]
    base = re.sub(r"[^a-zA-Z0-9_]", "", (user.username or f"u{user.id}")).lower() or f"u{user.id}"
    sticker_obj = InputSticker(sticker=file_arg, emoji_list=[emoji], format=fmt)

    # 🔑 Duplicate check ke liye naye sticker ka file_unique_id nikal lo
    new_file_unique_id = None
    try:
        if isinstance(file_arg, str):  # agar file_id hai
            f = await ctx.bot.get_file(file_arg)
            new_file_unique_id = f.file_unique_id
    except Exception:
        pass

    async def _create(part: int):
        name = f"kang{part}_{base}_by_{me.username}"
        title = f"{user.first_name or 'My'}'s Pack" + (f" {part}" if part > 1 else "")
        await ctx.bot.create_new_sticker_set(me.id, name, title[:64], stickers=[sticker_obj])
        return name, title

    pack_part = 1
    pack_title = ""
    try:
        rec = await dbase.kang_get(user.id)
        if rec and rec.get("name"):
            pack_part = rec.get("part", 1)
            pack_name = rec["name"]
            # 🔍 Existing pack ka title nikal lo refresh ke liye
            try:
                ss = await ctx.bot.get_sticker_set(pack_name)
                pack_title = ss.title
            except Exception:
                pack_title = f"{user.first_name or 'My'}'s Pack"

            # 🔍 Duplicate check
            if new_file_unique_id:
                try:
                    ss = await ctx.bot.get_sticker_set(pack_name)
                    existing_ids = {st.file_unique_id for st in ss.stickers}
                    if new_file_unique_id in existing_ids:
                        await say(ctx, chat.id, T("⚠️ Ye sticker pehle se tumhare pack me hai!"), reply_to=msg.message_id)
                        return
                except Exception:
                    pass

            try:
                await ctx.bot.add_sticker_to_set(me.id, pack_name, sticker=sticker_obj)
                name, count = pack_name, rec.get("count", 0) + 1
                pack_part = rec.get("part", 1)
                await dbase.kang_set(user.id, name, count, part=pack_part, format=rec.get("format", fmt))

                # 🔄 FORCE REFRESH: title wapas set karo
                try:
                    await ctx.bot.set_sticker_set_title(name, title=pack_title)
                    log.info("[sticker] pack refreshed via title: %s", name)
                except Exception as e:
                    log.warning("[sticker] title refresh failed: %s", e)

                # Thumbnail update (optional)
                try:
                    await ctx.bot.set_sticker_set_thumbnail(me.id, name, format=fmt, thumbnail=sticker_obj.sticker)
                except Exception as e:
                    log.warning("[sticker] thumbnail refresh failed: %s", e)

            except TelegramError as e:
                s = str(e).lower()
                if "invalid" in s or "too much" in s or "too many" in s:
                    part = rec.get("part", 1) + 1
                    name, pack_title = await _create(part)
                    count = 1
                    pack_part = part
                    await dbase.kang_set(user.id, name, count, part=part, format=fmt)
                    try:
                        await ctx.bot.set_sticker_set_thumbnail(me.id, name, format=fmt, thumbnail=sticker_obj.sticker)
                    except Exception:
                        pass
                else:
                    raise
        else:
            name, pack_title = await _create(1)
            count = 1
            pack_part = 1
            await dbase.kang_set(user.id, name, count, part=1, format=fmt)
            try:
                await ctx.bot.set_sticker_set_thumbnail(me.id, name, format=fmt, thumbnail=sticker_obj.sticker)
            except Exception:
                pass
    except TelegramError as e:
        err_msg = str(e).lower()
        if "peer_id_invalid" in err_msg or "user not found" in err_msg or "chat not found" in err_msg:
            bot_username = ctx.application.bot_data["me"].username
            kb = InlineKeyboardMarkup([[
                B("🚀 𝙎𝙩𝙖𝙧𝙩 𝘽𝙤𝙩 𝙞𝙣 𝙋𝙈", url=f"https://t.me/{bot_username}?start=true", style="success")
            ]])
            await ctx.bot.send_message(
                chat.id,
                q(T("❌ Sticker pack banane ke liye pehle mujhe PM me /start karna padega!\n\nNeeche button dabao, bot start karo, aur phir se .kang try karo.")),
                parse_mode="HTML",
                reply_to_message_id=msg.message_id,
                allow_sending_without_reply=True,
                reply_markup=kb
            )
        else:
            await say(ctx, chat.id, T("kang failed:") + f" {esc(e)}", reply_to=msg.message_id)
        return

    await dbase.kang_upsert_pack(user.id, name, count, pack_part, fmt)

    link = f"https://t.me/addstickers/{name}"
    kb = InlineKeyboardMarkup([[
        B("📦 𝙑𝙞𝙚𝙬 𝙥𝙖𝙘𝙠", url=link, style="primary")
    ]])
    await ctx.bot.send_message(
        chat.id,
        q(T("✅ added to your pack ({c} stickers so far).", c=count)),
        parse_mode="HTML",
        reply_to_message_id=msg.message_id,
        allow_sending_without_reply=True,
        reply_markup=kb,
    )


# ───────── .kangs (list + delete) ─────────

def _pack_icon(fmt: str) -> str:
    return {"static": "🖼", "video": "🎬", "animated": "✨"}.get(fmt, "📦")


def _short_pack_label(pack: dict) -> str:
    name = pack.get("name", "")
    m = re.match(r"kang(\d+)_", name)
    part = m.group(1) if m else "?"
    icon = _pack_icon(pack.get("format", "static"))
    return f"{icon} Pack {part}"


def _build_kangs_view(packs: list, note: str | None = None) -> tuple[str, InlineKeyboardMarkup | None]:
    if not packs:
        return T("you don't have any sticker packs yet.\nuse /kang to create your first one!"), None
    lines = [T("<b>✦ your sticker packs</b>\n")]
    rows = []
    for i, p in enumerate(packs):
        name = p.get("name", "")
        count = p.get("count", 0)
        fmt = p.get("format", "static")
        icon = _pack_icon(fmt)
        short = _short_pack_label(p)
        lines.append(f"{icon} <b>{esc(short)}</b> — {count} stickers")
        rows.append([
            B(f"{icon} 𝙑𝙞𝙚𝙬 {short}", url=f"https://t.me/addstickers/{name}", style="primary"),
            B("🗑 𝘿𝙚𝙡𝙚𝙩𝙚", data=f"kdel:{i}", style="danger"),
        ])
    if note:
        lines.append(f"\n{note}")
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def kangs_cmd(update, ctx):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    packs = await dbase.kang_list_packs(user.id)

    # Auto-migrate old single-pack layout into the packs list
    if not packs:
        rec = await dbase.kang_get(user.id)
        if rec and rec.get("name"):
            await dbase.kang_upsert_pack(
                user.id, rec["name"], rec.get("count", 0),
                rec.get("part", 1), rec.get("format", "static"),
            )
            packs = await dbase.kang_list_packs(user.id)

    text, kb = _build_kangs_view(packs)
    await say(ctx, chat.id, text, kb=kb, reply_to=msg.message_id)


async def kangs_delete_cb(update, ctx):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    try:
        idx = int(query.data.split(":", 1)[1])
    except Exception:
        return

    packs = await dbase.kang_list_packs(user.id)
    if idx < 0 or idx >= len(packs):
        try:
            await query.edit_message_text(q(T("❌ that pack no longer exists.")), parse_mode="HTML")
        except Exception:
            pass
        return

    pack = packs[idx]
    name = pack["name"]

    # Delete all stickers in the pack (set auto-deletes on the last one)
    deleted = 0
    failed = 0
    try:
        ss = await ctx.bot.get_sticker_set(name)
        for st in ss.stickers:
            try:
                await ctx.bot.delete_sticker_from_set(st.file_id)
                deleted += 1
            except TelegramError as e:
                failed += 1
                log.debug("[kangs] couldn't delete sticker: %s", e)
    except TelegramError as e:
        log.warning("[kangs] couldn't fetch pack %s: %s", name, e)

    await dbase.kang_delete_pack(user.id, name)

    # Refresh the list view
    packs = await dbase.kang_list_packs(user.id)
    note = T("🗑 deleted <b>{n}</b> ({c} stickers)", n=esc(_short_pack_label(pack)), c=deleted)
    if failed:
        note += T(" — {f} failed", f=failed)

    if not packs:
        try:
            await query.edit_message_text(q(note + "\n\n" + T("you have no packs left.")), parse_mode="HTML")
        except Exception:
            pass
        return

    text, kb = _build_kangs_view(packs, note=note)
    try:
        await query.edit_message_text(q(text), parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        log.debug("[kangs] edit failed: %s", e)


# ───────── .unkang ─────────

async def unkang_cmd(update, ctx):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    src = msg.reply_to_message
    if not src or not src.sticker:
        await say(ctx, chat.id, T("reply to a sticker to remove it from your pack."), reply_to=msg.message_id)
        return

    s = src.sticker
    if not s.set_name:
        await say(ctx, chat.id, T("that sticker isn't part of any pack."), reply_to=msg.message_id)
        return

    me = ctx.application.bot_data["me"]
    base = re.sub(r"[^a-zA-Z0-9_]", "", (user.username or f"u{user.id}")).lower() or f"u{user.id}"
    if f"_by_{me.username}" not in s.set_name or base not in s.set_name:
        await say(ctx, chat.id, T("that sticker isn't in your pack."), reply_to=msg.message_id)
        return

    try:
        await ctx.bot.delete_sticker_from_set(s.file_id)
    except TelegramError as e:
        await say(ctx, chat.id, T("❌ couldn't remove that sticker:") + f" {esc(e)}", reply_to=msg.message_id)
        return

    # Decrement count in DB if it matches the replied sticker's pack
    rec = await dbase.kang_get(user.id)
    if rec and rec.get("name") == s.set_name:
        new_count = max(0, rec.get("count", 1) - 1)
        await dbase.kang_set(
            user.id, rec["name"], new_count,
            part=rec.get("part", 1), format=rec.get("format"),
        )
        # Also update the packs list entry
        packs = await dbase.kang_list_packs(user.id)
        for p in packs:
            if p.get("name") == s.set_name:
                await dbase.kang_upsert_pack(
                    user.id, p["name"], new_count,
                    p.get("part", 1), p.get("format", "static"),
                )
                break

    await say(ctx, chat.id, T("✅ sticker removed from your pack."), reply_to=msg.message_id)


def register(app):
    dual_command(app, "q", q_cmd)
    dual_command(app, "qr", qr_cmd)
    dual_command(app, "kang", kang_cmd)
    dual_command(app, "kangs", kangs_cmd)
    dual_command(app, "unkang", unkang_cmd)
    app.add_handler(CallbackQueryHandler(kangs_delete_cb, pattern=r"^kdel:"))
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.REPLY & (filters.TEXT | filters.CAPTION),
            _cache_reply_watcher,
            block=False,
        ),
        group=-100,
                )
    
