"""Sticker plugin:
.q / .qr  — render the replied-to message as a Telegram-style quote sticker
.kang     — steal a replied sticker/photo into the user's own auto-growing pack

Quote generation flow:
  1. Self-hosted quote-api (QUOTE_API_URL env, default http://127.0.0.1:3000/generate)
  2. Public fallback APIs (QUOTE_API_FALLBACKS env)
  3. Local Pillow rendering (uses ./fonts/DejaVuSans*.ttf)

The .kang command is unchanged — always uses Pillow + DejaVu fonts."""
import asyncio
import base64
import io
import logging
import os
import re

import aiohttp
from PIL import Image, ImageDraw, ImageFont
from telegram import InputSticker
from telegram.error import TelegramError

import database as dbase
from common import T, dual_command, esc, say

log = logging.getLogger("sticker")

HELP_TXT = (
    "<b>✦ stickers</b>\n\n"
    "/q (or .q) — reply to any message to turn it into a quote sticker\n"
    "/qr (or .qr) — same, but sent as a reply to the original message\n"
    "/kang (or .kang) [emoji] — reply to a sticker or photo to add it to your "
    "own sticker pack. the pack is created the first time and reused after that — "
    "every later .kang just adds to it (a new part is started automatically if it fills up)."
)
COMMANDS = [("q", "Quote a message as a sticker"), ("kang", "Steal a sticker into your pack")]

MAX_SIDE = 512
EMOJI_RE = re.compile(r"^[\U0001F000-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF]+$")

# ───────── config (env-driven) ─────────
QUOTE_API_URL = os.getenv("QUOTE_API_URL", "http://127.0.0.1:3000/generate")
QUOTE_API_FALLBACKS = [
    u.strip() for u in os.getenv(
        "QUOTE_API_FALLBACKS",
        "https://bot.lyo.su/quote/generate"
    ).split(",") if u.strip()
]
QUOTE_BG = os.getenv("QUOTE_BG", "#1b1429")
API_TIMEOUT = float(os.getenv("QUOTE_API_TIMEOUT", "8"))
TOTAL_TIMEOUT = float(os.getenv("QUOTE_TOTAL_TIMEOUT", "20"))

# ───────── Pillow fallback config ─────────
BUBBLE_BG = (43, 43, 63, 255)
PEER_COLORS = [
    (225, 112, 118), (250, 167, 116), (166, 149, 231),
    (123, 200, 98), (110, 201, 203), (101, 170, 221), (238, 122, 174),
]
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
FONT_REGULAR = os.path.join(FONT_DIR, "DejaVuSans.ttf")
FONT_BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")

# Entity types quote-api understands. Anything else is dropped.
_SUPPORTED_ENTITY_TYPES = {
    "bold", "italic", "underline", "strikethrough", "spoiler",
    "code", "pre", "text_link", "text_mention", "mention",
    "hashtag", "cashtag", "bot_command", "url", "email",
    "phone_number", "custom_emoji",
}


# ───────── Pillow helpers (used by fallback + .kang) ─────────

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


def _fit_512(img: Image.Image) -> Image.Image:
    w, h = img.size
    scale = MAX_SIDE / max(w, h)
    return img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def _peer_color(user_id: int):
    return PEER_COLORS[abs(user_id) % len(PEER_COLORS)]


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


async def _profile_photo_bytes(ctx, user) -> bytes | None:
    """Fetch profile photo with a hard timeout — never hangs."""
    try:
        async def _fetch():
            photos = await ctx.bot.get_user_profile_photos(user.id, limit=1)
            if not photos.total_count:
                return None
            file_id = photos.photos[0][-1].file_id
            tgfile = await ctx.bot.get_file(file_id)
            return bytes(await tgfile.download_as_bytearray())
        return await asyncio.wait_for(_fetch(), timeout=6.0)
    except (asyncio.TimeoutError, TelegramError, OSError) as e:
        log.debug("profile photo fetch failed for %s: %s", user.id, e)
        return None


# ───────── entity extraction ─────────

def _extract_entities(msg) -> list[dict]:
    """Convert Telegram MessageEntity objects to quote-api's JSON format."""
    raw = list(msg.entities or []) + list(msg.caption_entities or [])
    out = []
    for e in raw:
        if e.type not in _SUPPORTED_ENTITY_TYPES:
            continue
        item = {"type": e.type, "offset": e.offset, "length": e.length}
        if getattr(e, "url", None):
            item["url"] = e.url
        if getattr(e, "language", None):
            item["language"] = e.language
        if getattr(e, "user", None) and e.user:
            item["user"] = {"id": e.user.id, "name": e.user.full_name}
        if getattr(e, "custom_emoji_id", None):
            item["custom_emoji_id"] = e.custom_emoji_id
        out.append(item)
    return out


def _reply_context(msg) -> dict | None:
    """If the replied message itself is a reply, pass minimal context."""
    if not msg.reply_to_message:
        return None
    r = msg.reply_to_message
    sender = r.from_user
    return {
        "name": (sender.first_name if sender else "User") or "User",
        "text": r.text or r.caption or "",
        "entities": _extract_entities(r),
    }


# ───────── API path ─────────

async def _try_one_api(session, url, payload) -> bytes | None:
    try:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                log.warning("⚠️ %s returned %s", url, resp.status)
                return None

            ctype = (resp.headers.get("Content-Type") or "").lower()
            data = await resp.read()
            if not data or len(data) < 100:
                log.warning("⚠️ %s returned empty/short body", url)
                return None

            if "application/json" in ctype:
                try:
                    j = await resp.json(content_type=None)
                except Exception:
                    log.warning("⚠️ %s claimed JSON but wasn't parseable", url)
                    return None
                b64 = j.get("result") or j.get("image") or j.get("data")
                if not b64:
                    log.warning("⚠️ %s JSON had no image field", url)
                    return None
                try:
                    return base64.b64decode(b64)
                except Exception as e:
                    log.warning("⚠️ %s base64 decode failed: %s", url, e)
                    return None

            log.info("✅ quote built via %s (%d bytes, %s)", url, len(data), ctype or "binary")
            return data
    except Exception as e:
        log.warning("⚠️ %s failed: %s", url, e)
        return None


async def _build_quote_via_api(ctx, src_msg, sender) -> bytes | None:
    text = src_msg.text or src_msg.caption or ""
    if not text:
        return None

    photo_bytes = await _profile_photo_bytes(ctx, sender)
    photo_url = (
        f"data:image/jpeg;base64,{base64.b64encode(photo_bytes).decode()}"
        if photo_bytes else ""
    )

    payload = {
        "type": "quote",
        "format": "webp",
        "backgroundColor": QUOTE_BG,
        "messages": [{
            "entities": _extract_entities(src_msg),
            "avatar": True,
            "from": {
                "id": sender.id,
                "name": sender.first_name or sender.full_name or "User",
                "photo": {"url": photo_url} if photo_url else {},
            },
            "text": text,
            "replyMessage": _reply_context(src_msg) or {},
        }],
    }

    urls = [QUOTE_API_URL] + QUOTE_API_FALLBACKS
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        log.info("[sticker] trying %d APIs concurrently", len(urls))
        tasks = [_try_one_api(session, url, payload) for url in urls]
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

    raw_text = src_msg.text or src_msg.caption or "[media]"
    text = re.sub(r"\s+", " ", raw_text).strip()
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

    photo = await _profile_photo_bytes(ctx, sender)
    avatar = Image.new("RGBA", (avatar_size, avatar_size), (0, 0, 0, 0))
    if photo:
        try:
            av = Image.open(io.BytesIO(photo)).convert("RGBA").resize((avatar_size, avatar_size))
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


# ───────── shared ─────────

async def _build_quote_sticker(ctx, src_msg, sender) -> bytes:
    try:
        data = await _build_quote_via_api(ctx, src_msg, sender)
        if data:
            return data
    except Exception as e:
        log.warning("[sticker] API path raised: %s", e)
    log.warning("[sticker] all APIs failed — using local Pillow fallback")
    return await _build_quote_via_pillow(ctx, src_msg, sender)


async def _quote_and_send(update, ctx, as_reply: bool):
    msg, chat = update.effective_message, update.effective_chat
    src = msg.reply_to_message
    if not src:
        await say(ctx, chat.id, T("reply to a message with /q to turn it into a sticker."), reply_to=msg.message_id)
        return
    if not (src.text or src.caption):
        await say(ctx, chat.id, T("only text messages can be quoted."), reply_to=msg.message_id)
        return

    sender = src.from_user or msg.from_user
    status = await say(ctx, chat.id, T("⏳ generating quote..."), reply_to=msg.message_id)

    try:
        sticker_bytes = await asyncio.wait_for(
            _build_quote_sticker(ctx, src, sender), timeout=TOTAL_TIMEOUT
        )
    except asyncio.TimeoutError:
        log.error("[sticker] total timeout reached")
        try:
            await status.edit_text(T("❌ quote generation timed out. try again."))
        except Exception:
            pass
        return
    except Exception as e:
        log.error("[sticker] failed: %s", e, exc_info=True)
        try:
            await status.edit_text(T("❌ couldn't build that sticker:") + f" {esc(e)}")
        except Exception:
            pass
        return

    try:
        await status.delete()
    except Exception:
        pass

    kwargs = {"reply_to_message_id": src.message_id} if as_reply else {}
    try:
        await ctx.bot.send_sticker(chat.id, io.BytesIO(sticker_bytes), **kwargs)
        log.info("[sticker] sticker sent to chat %s", chat.id)
    except Exception as e:
        log.error("[sticker] send failed: %s", e)


async def q_cmd(update, ctx):
    await _quote_and_send(update, ctx, as_reply=False)


async def qr_cmd(update, ctx):
    await _quote_and_send(update, ctx, as_reply=True)


# ───────── .kang (unchanged) ─────────

async def _photo_to_sticker_file(ctx, photo):
    tgfile = await ctx.bot.get_file(photo.file_id)
    raw = await tgfile.download_as_bytearray()
    im = Image.open(io.BytesIO(bytes(raw))).convert("RGBA")
    im = _fit_512(im)
    buf = io.BytesIO()
    buf.name = "kang.png"
    im.save(buf, "PNG")
    buf.seek(0)
    return buf


async def kang_cmd(update, ctx):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    src = msg.reply_to_message
    if not src or not (src.sticker or src.photo):
        await say(ctx, chat.id, T("reply to a sticker or a photo with /kang to steal it into your pack."), reply_to=msg.message_id)
        return

    emoji = ctx.args[0] if ctx.args and EMOJI_RE.match(ctx.args[0]) else None
    if src.sticker:
        s = src.sticker
        file_arg = s.file_id
        fmt = "video" if s.is_video else ("animated" if s.is_animated else "static")
        emoji = emoji or s.emoji or "🤔"
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
