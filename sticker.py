"""Sticker plugin:
.q / .qr  — render the replied-to message as a Telegram-style quote sticker
.kang     — steal a replied sticker/photo into the user's own auto-growing pack

Quote commands:
  .q              → normal quote sticker
  .q r / .q reply → quote sticker sent as a reply to the source message
  .qr             → same as .q r (shortcut)

Quote generation flow:
  1. Self-hosted quote-api (QUOTE_API env or common.py default)
  2. Public fallback APIs (QUOTE_API_FALLBACKS env)
  3. Local Pillow rendering (uses ./fonts/DejaVuSans*.ttf)

.kang handling:
  • Photos           → download, square-crop 512x512, WEBP
  • Static stickers  → pass file_id directly
  • Video stickers   → pass file_id directly (WEBM)
  • Animated stickers→ pass file_id directly (TGS)

Avatar: we pass Telegram's direct file URL (not a data URI) because the
quote-api uses axios, which cannot fetch data: URIs. The URL contains the bot
token, but the API runs on the same VPS, so the token never leaves the host."""
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
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont
from telegram import InputSticker, MessageEntity
from telegram.error import TelegramError

import database as dbase
from common import (
    T, dual_command, esc, say,
    QUOTE_API, QUOTE_API_FALLBACKS, QUOTE_BG,
    QUOTE_TIMEOUT, QUOTE_TOTAL_TIMEOUT,
)

log = logging.getLogger("sticker")

HELP_TXT = (
    "<b>✦ stickers</b>\n\n"
    "/q (or .q) — reply to any message to turn it into a quote sticker\n"
    "/q r (or .q r) — same, but sent as a reply to the original message\n"
    "/qr (or .qr) — shortcut for .q r\n"
    "/kang (or .kang) [emoji] — reply to a sticker or photo to add it to your "
    "own sticker pack. the pack is created the first time and reused after that — "
    "every later .kang just adds to it (a new part is started automatically if it fills up)."
)
COMMANDS = [("q", "Quote a message as a sticker"), ("kang", "Steal a sticker into your pack")]

MAX_SIDE = 512
EMOJI_RE = re.compile(r"^[\U0001F000-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF]+$")

# ───────── runtime config ─────────
_FALLBACKS = [u.strip() for u in (QUOTE_API_FALLBACKS or "").split(",") if u.strip()]
TOTAL_TIMEOUT = QUOTE_TOTAL_TIMEOUT

COOLDOWN = 8.0                       # seconds between quotes per user
_AVATAR_TTL = 600                    # 10 min avatar URL cache

# ───────── runtime caches ─────────
_last_quote: dict[int, float] = {}
_avatar_cache: dict[int, tuple[float, str | None]] = {}

# ───────── entity support ─────────
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


# ───────── local Pillow helpers (fallback + .kang) ─────────
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
    """Crop to a centre square, then resize to exactly 512x512."""
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
    """Download the user's big profile photo. Never hangs."""
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


async def _avatar_url(ctx, user_id: int) -> str | None:
    """Return a URL to the user's Telegram profile photo that the quote-api
    can fetch with axios.

    Data URIs (data:image/jpeg;base64,...) do NOT work — the API silently
    drops them, which is why avatars were missing from the quote sticker.
    We use Telegram's direct file URL instead."""
    now = time.time()
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


def _build_message(msg, avatar: str | None = None) -> dict:
    block = {
        "entities": _extract_entities(msg),
        "avatar": True,
        "from": _from_block(getattr(msg, "from_user", None)),
        "text": _text_of(msg),
    }
    if avatar:
        block["from"]["photo"] = {"url": avatar}
    reply = getattr(msg, "reply_to_message", None)
    if reply is not None and _text_of(reply):
        block["replyMessage"] = {
            "entities": _extract_entities(reply),
            "from": _from_block(getattr(reply, "from_user", None)),
            "text": _text_of(reply),
        }
    return block


def _build_payload(msg, avatar: str | None = None) -> dict:
    return {
        "type": "quote",
        "format": "webp",
        "backgroundColor": QUOTE_BG,
        "width": 512,
        "height": 512,
        "scale": 1,
        "emojiBrand": "apple",
        "messages": [_build_message(msg, avatar)],
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

        # LyoSU quote-api returns JSON: {"ok":true,"result":{"image":"<base64>"}}
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


async def _build_quote_via_api(ctx, src_msg, sender) -> bytes | None:
    text = _text_of(src_msg)
    if not text:
        return None

    avatar_url = await _avatar_url(ctx, sender.id)
    log.info("[sticker] avatar url: %s", "yes" if avatar_url else "none")
    payload = _build_payload(src_msg, avatar_url)

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


# ───────── shared builder ─────────

async def _build_quote_sticker(ctx, src_msg, sender) -> bytes:
    try:
        data = await _build_quote_via_api(ctx, src_msg, sender)
        if data:
            return data
    except Exception as e:
        log.warning("[sticker] API path raised: %s", e)
    log.warning("[sticker] all APIs failed — using local Pillow fallback")
    return await _build_quote_via_pillow(ctx, src_msg, sender)


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
            _build_quote_sticker(ctx, src, sender), timeout=TOTAL_TIMEOUT
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

    # Try to send as reply; if Telegram refuses (deleted msg / restrictions),
    # fall back to a normal sticker send so the user still gets their quote.
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
    """Supports both:
        .q              → normal quote
        .q r / .q reply → quote as a reply to the source message
    """
    args = [a.lower() for a in (ctx.args or [])]
    as_reply = bool(args) and args[0] in ("r", "reply", "re")
    await _quote_and_send(update, ctx, as_reply=as_reply)


async def qr_cmd(update, ctx):
    await _quote_and_send(update, ctx, as_reply=True)


# ───────── .kang helpers ─────────

async def _photo_to_sticker_file(ctx, photo):
    """Download a photo, crop to a centre square, resize to 512x512, WEBP."""
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
        # Pass file_id directly — Telegram already has this file. Works for
        # static, video, AND animated stickers without re-uploading.
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
