"""Sticker plugin — quote generator (ported from original developer's quotly.py)
+ kang system with MongoDB.

.q / .qr  — render the replied-to message as a Telegram-style quote sticker
.kang     — steal a replied sticker/photo into the user's own auto-growing pack
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

import httpx
from telegram import InputSticker, MessageEntity
from telegram.error import TelegramError

import database as dbase
from common import (
    T, dual_command, esc, say,
    QUOTE_BG, QUOTE_TIMEOUT, QUOTE_TOTAL_TIMEOUT,
)

log = logging.getLogger("sticker")

# ✅ Original developer ka API (quotly.py se)
QUOTE_API = "https://bot.lyo.su/quote/generate"

HELP_TXT = (
    "<b>✦ stickers</b>\n\n"
    "/q (or .q) — reply to any message to turn it into a quote sticker\n"
    "/q r (or .q r) — same, but sent as a reply to the original message\n"
    "/qr (or .qr) — shortcut for .q r\n"
    "/kang (or .kang) [emoji] — reply to a sticker or photo to add it to your "
    "own sticker pack."
)
COMMANDS = [("q", "Quote a message as a sticker"), ("kang", "Steal a sticker into your pack")]

MAX_SIDE = 512
EMOJI_RE = re.compile(r"^[\U0001F000-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF]+$")

TOTAL_TIMEOUT = QUOTE_TOTAL_TIMEOUT
COOLDOWN = 8.0

_last_quote: dict[int, float] = {}

# ✅ Original developer ke entity types (quotly.py se)
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
    MessageEntity.PHONE_NUMBER: "phone_number",
    MessageEntity.MENTION: "mention",
    MessageEntity.CASHTAG: "cashtag",
    MessageEntity.HASHTAG: "hashtag",
    MessageEntity.EMAIL: "email",
    MessageEntity.URL: "url",
    MessageEntity.BOT_COMMAND: "bot_command",
}

_KEEP_ENTITIES = {
    "bold", "italic", "underline", "strikethrough", "spoiler",
    "code", "pre", "blockquote", "text_link", "text_mention",
    "phone_number", "mention", "cashtag", "hashtag", "email",
    "url", "bot_command",
}


# ───────── Pillow helpers (fallback + kang) ─────────
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


def _peer_color(user_id: int):
    return PEER_COLORS[abs(user_id) % len(PEER_COLORS)]


def _fit_512(img):
    w, h = img.size
    scale = MAX_SIDE / max(w, h)
    return img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def _square_512(img):
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


async def _fetch_avatar_bytes(ctx, user_id: int):
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


# ✅ Original developer ka exact `from` block (quotly.py se)
def _from_block(user) -> dict:
    if user is None:
        return {
            "id": 0,
            "first_name": "Deleted Account",
            "last_name": None,
            "username": None,
            "language_code": "en",
            "title": "Deleted Account",
            "name": "Deleted Account",
            "type": "private",
        }
    first_name = user.first_name or "User"
    last_name = getattr(user, "last_name", None)
    username = getattr(user, "username", None)
    name = " ".join(p for p in [first_name, last_name] if p).strip() or "User"
    return {
        "id": int(user.id or 0),
        "first_name": first_name,
        "last_name": last_name,
        "username": username,
        "language_code": "en",
        "title": name,
        "name": name,
        "type": "private",
    }


# ✅ Original developer ka exact reply block (quotly.py se)
def _build_reply_block(reply) -> dict | None:
    if reply is None:
        return None
    text = _text_of(reply)
    if not text:
        return None
    sender = getattr(reply, "from_user", None)
    if sender is None:
        name = "Deleted Account"
    else:
        parts = [sender.first_name or "", getattr(sender, "last_name", "") or ""]
        name = " ".join(p for p in parts if p).strip() or "Deleted Account"
    return {
        "name": name,
        "text": text,
        "chatId": int(getattr(reply, "chat_id", 0) or 0),
    }


# ✅ Original developer ka exact message block
def _build_message(msg, parent_msg=None) -> dict:
    block = {
        "entities": _extract_entities(msg),
        "chatId": int(getattr(msg, "from_user", None).id if getattr(msg, "from_user", None) else 0),
        "avatar": True,
        "from": _from_block(getattr(msg, "from_user", None)),
        "text": _text_of(msg),
        "replyMessage": _build_reply_block(parent_msg) or {},
    }
    return block


# ✅ Original developer ke exact dimensions (quotly.py se)
def _build_payload(msg, parent_msg=None) -> dict:
    bg = QUOTE_BG or "#1b1429"
    return {
        "type": "quote",
        "format": "webp",
        "backgroundColor": bg,
        "width": 512,
        "height": 768,
        "scale": 2,
        "messages": [_build_message(msg, parent_msg)],
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


async def _build_quote_via_api(ctx, src_msg, parent_msg=None) -> bytes | None:
    if not _text_of(src_msg):
        return None

    payload = _build_payload(src_msg, parent_msg)
    msg0 = payload["messages"][0]
    if msg0.get("replyMessage"):
        log.info("[sticker] nested reply: %r", msg0["replyMessage"].get("text", "")[:50])
    else:
        log.info("[sticker] no nested reply")

    timeout = httpx.Timeout(QUOTE_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await _try_one_api(client, QUOTE_API, payload)


# ───────── Pillow fallback ─────────

async def _build_quote_via_pillow(ctx, src_msg, sender, parent_msg=None) -> bytes:
    log.info("[sticker] rendering locally with Pillow")
    W = 512
    outer_pad = 12
    
    avatar_size = 92
    gap = 14
    inner_pad = 22
    font_name = _load_font(True, 30)
    font_text = _load_font(False, 36)
    raw_text = _text_of(src_msg) or "[media]"
    text = raw_text
    sender_name = re.sub(r"\s+", " ", (sender.full_name or "Unknown")).strip()

    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    bubble_x = outer_pad + avatar_size + gap
    bubble_w = W - bubble_x - outer_pad
    text_w = bubble_w - inner_pad * 2
    name_lines = _wrap(probe, sender_name, font_name, text_w)
    name_line_h = font_name.getbbox("Ag")[3] + 6
    text_lines = _wrap(probe, text, font_text, text_w)
    text_line_h = font_text.getbbox("Ag")[3] + 12
    gap_name_text = 12
    body_h = name_line_h * len(name_lines) + gap_name_text + text_line_h * len(text_lines)
    main_bubble_h = body_h + inner_pad * 2

    nested_bubble_h = 0
    nested_data = None
    if parent_msg:
        p_text = _text_of(parent_msg) or "[media]"
        p_sender = getattr(parent_msg, "from_user", None)
        p_name = re.sub(r"\s+", " ", (p_sender.full_name if p_sender else "Unknown")).strip()
        
        p_avatar_size = 64
        p_gap = 10
        p_inner_pad = 16
        p_font_name = _load_font(True, 22)
        p_font_text = _load_font(False, 26)
        
        p_bubble_x = outer_pad + p_avatar_size + p_gap
        p_bubble_w = W - p_bubble_x - outer_pad
        p_text_w = p_bubble_w - p_inner_pad * 2
        
        p_name_lines = _wrap(probe, p_name, p_font_name, p_text_w)
        p_name_line_h = p_font_name.getbbox("Ag")[3] + 4
        p_text_lines = _wrap(probe, p_text, p_font_text, p_text_w)
        p_text_line_h = p_font_text.getbbox("Ag")[3] + 8
        p_gap_name_text = 8
        
        p_body_h = p_name_line_h * len(p_name_lines) + p_gap_name_text + p_text_line_h * len(p_text_lines)
        nested_bubble_h = p_body_h + p_inner_pad * 2
        
        nested_data = {
            "x": p_bubble_x, "w": p_bubble_w, "h": nested_bubble_h,
            "name_lines": p_name_lines, "text_lines": p_text_lines,
            "font_name": p_font_name, "font_text": p_font_text,
            "line_h_name": p_name_line_h, "line_h_text": p_text_line_h,
            "gap_name_text": p_gap_name_text, "inner_pad": p_inner_pad,
            "avatar_size": p_avatar_size, "sender": p_sender
        }

    total_h = outer_pad * 2 + main_bubble_h + (nested_bubble_h + 10 if nested_bubble_h else 0)
    
    img = Image.new("RGBA", (W, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    current_y = outer_pad

    if nested_data:
        nd = nested_data
        draw.rounded_rectangle(
            (nd["x"], current_y, nd["x"] + nd["w"], current_y + nd["h"]),
            radius=20, fill=BUBBLE_BG,
        )
        ty = current_y + nd["inner_pad"]
        n_color = _peer_color(nd["sender"].id) if nd["sender"] else (255, 255, 255)
        for ln in nd["name_lines"]:
            draw.text((nd["x"] + nd["inner_pad"], ty), ln, font=nd["font_name"], fill=n_color)
            ty += nd["line_h_name"]
        ty += nd["gap_name_text"]
        for ln in nd["text_lines"]:
            draw.text((nd["x"] + nd["inner_pad"], ty), ln, font=nd["font_text"], fill=(255, 255, 255))
            ty += nd["line_h_text"]
            
        p_photo = await _fetch_avatar_bytes(ctx, nd["sender"].id) if nd["sender"] else None
        p_avatar = Image.new("RGBA", (nd["avatar_size"], nd["avatar_size"]), (0, 0, 0, 0))
        if p_photo:
            try:
                av = Image.open(io.BytesIO(p_photo)).convert("RGBA")
                w, h = av.size
                side = min(w, h)
                av = av.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
                av = av.resize((nd["avatar_size"], nd["avatar_size"]), Image.LANCZOS)
                mask = Image.new("L", (nd["avatar_size"], nd["avatar_size"]), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, nd["avatar_size"], nd["avatar_size"]), fill=255)
                av.putalpha(mask)
                p_avatar = av
            except Exception:
                p_photo = None
        if not p_photo:
            d = ImageDraw.Draw(p_avatar)
            d.ellipse((0, 0, nd["avatar_size"], nd["avatar_size"]), fill=_peer_color(nd["sender"].id) if nd["sender"] else (100,100,100))
            f = _load_font(True, nd["avatar_size"] // 2)
            words = re.findall(r"[A-Za-z]+", nd["sender"].full_name if nd["sender"] else "?")
            initials = (words[0][0] + (words[1][0] if len(words) > 1 else "")).upper() if words else "?"
            bbox = d.textbbox((0, 0), initials, font=f)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            d.text(((nd["avatar_size"] - tw) / 2 - bbox[0], (nd["avatar_size"] - th) / 2 - bbox[1]),
                   initials, font=f, fill=(255, 255, 255))
        img.paste(p_avatar, (outer_pad, current_y + (nd["h"] - nd["avatar_size"]) // 2), p_avatar)
        current_y += nested_bubble_h + 10

    draw.rounded_rectangle(
        (bubble_x, current_y, bubble_x + bubble_w, current_y + main_bubble_h),
        radius=26, fill=BUBBLE_BG,
    )
    ty = current_y + inner_pad
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
    img.paste(avatar, (outer_pad, current_y + (main_bubble_h - avatar_size) // 2), avatar)

    img = _fit_512(img)
    out = io.BytesIO()
    img.save(out, "WEBP")
    return out.getvalue()


async def _build_quote_sticker(ctx, src_msg, sender, parent_msg=None) -> bytes:
    try:
        data = await _build_quote_via_api(ctx, src_msg, parent_msg)
        if data:
            return data
    except Exception as e:
        log.warning("[sticker] API path raised: %s", e)
    log.warning("[sticker] API failed — using local Pillow fallback")
    return await _build_quote_via_pillow(ctx, src_msg, sender, parent_msg)


# ───────── parent-message helper ─────────

async def _ensure_parent(ctx, chat_id: int, src):
    parent = getattr(src, "reply_to_message", None)
    if parent and getattr(parent, "message_id", None):
        log.info("[sticker] parent found via reply_to_message")
        return parent

    parent_id = getattr(src, "reply_to_message_id", None)
    if not parent_id:
        ext = getattr(src, "external_reply", None)
        if ext:
            parent_id = getattr(ext, "message_id", None)
    if not parent_id and parent:
        parent_id = getattr(parent, "message_id", None)

    if not parent_id:
        log.info("[sticker] no parent_id detected")
        return None

    log.info("[sticker] fetching parent_id: %s", parent_id)
    try:
        fetched = await ctx.bot.get_messages(chat_id=chat_id, message_ids=parent_id)
        if isinstance(fetched, list):
            fetched = fetched[0] if fetched else None
        return fetched
    except Exception as e:
        log.warning("[sticker] couldn't fetch parent %s: %s", parent_id, e)
        return None


# ───────── command handler ─────────

def _cooldown_left(uid: int) -> float:
    return max(0.0, _last_quote.get(uid, 0.0) + COOLDOWN - time.time())


async def _quote_and_send(update, ctx, as_reply: bool):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    src = msg.reply_to_message

    if not src:
        await say(ctx, chat.id, T("reply to a message with /q to turn it into a sticker."), reply_to=msg.message_id)
        return
    if not _text_of(src):
        await say(ctx, chat.id, T("only text messages can be quoted."), reply_to=msg.message_id)
        return

    parent_msg = await _ensure_parent(ctx, chat.id, src)
    if parent_msg:
        log.info("[sticker] parent text: %r", _text_of(parent_msg)[:60])
    else:
        log.info("[sticker] no parent message found")

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
            _build_quote_sticker(ctx, src, sender, parent_msg), timeout=TOTAL_TIMEOUT
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


async def q_cmd(update, ctx):
    args = [a.lower() for a in (ctx.args or [])]
    as_reply = bool(args) and args[0] in ("r", "reply", "re")
    await _quote_and_send(update, ctx, as_reply=as_reply)


async def qr_cmd(update, ctx):
    await _quote_and_send(update, ctx, as_reply=True)


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


# ───────── .kang (MongoDB system — aapka purana) ─────────

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

    async def _create(part: int):
        name = f"a{part}_{base}_by_{me.username}"
        title = f"{user.first_name or 'My'}'s Pack" + (f" {part}" if part > 1 else "")
        await ctx.bot.create_new_sticker_set(user.id, name, title[:64], stickers=[sticker_obj])
        return name

    try:
        rec = await dbase.kang_get(user.id)
        if rec and rec.get("name"):
            try:
                await ctx.bot.add_sticker_to_set(user.id, rec["name"], sticker=sticker_obj)
                name, count = rec["name"], rec.get("count", 0) + 1
                await dbase.kang_set(user.id, name, count, part=rec.get("part", 1))
            except TelegramError as e:
                s = str(e).lower()
                if "invalid" in s or "too much" in s or "too many" in s:
                    part = rec.get("part", 1) + 1
                    name = await _create(part)
                    count = 1
                    await dbase.kang_set(user.id, name, count, part=part)
                else:
                    raise
        else:
            name = await _create(1)
            count = 1
            await dbase.kang_set(user.id, name, count, part=1)
    except TelegramError as e:
        await say(ctx, chat.id, T("kang failed:") + f" {esc(e)}", reply_to=msg.message_id)
        return

    link = f"https://t.me/addstickers/{name}"
    await say(ctx, chat.id, T("✅ added to your pack ({c} stickers so far).\n{l}", c=count, l=esc(link)), reply_to=msg.message_id)


def register(app):
    dual_command(app, "q", q_cmd)
    dual_command(app, "qr", qr_cmd)
    dual_command(app, "kang", kang_cmd)
    
