"""Sticker plugin:
.q / .qr  — render the replied-to message as a quote sticker
.kang     — steal a replied sticker/photo into the user's own auto-growing pack

Quote generation tries several public APIs in order (lyo.su first, then a
mirror). If all APIs are down, it falls back to local Pillow rendering using
the bundled DejaVu fonts. So the command never fully fails."""
import base64
import io
import logging
import os
import re
import unicodedata

import aiohttp
from fontTools.ttLib import TTFont
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

# Public quote APIs, tried in order. First one wins.
QUOTE_APIS = [
    "https://bot.lyo.su/quote/generate",
    "https://quotes-api.talle.workers.dev/quote/generate",
    "https://quote-api.vercel.app/quote/generate",
]
QUOTE_BG = "#1b1429"

# ───────── local Pillow fallback config ─────────
BUBBLE_BG = (43, 43, 63, 255)
PEER_COLORS = [
    (225, 112, 118), (250, 167, 116), (166, 149, 231),
    (123, 200, 98), (110, 201, 203), (101, 170, 221), (238, 122, 174),
]
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
FONT_REGULAR = os.path.join(FONT_DIR, "DejaVuSans.ttf")
FONT_BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")

_NAME_LOOKALIKE = {
    '˹': '[', '˺': ']', '˻': '[', '˼': ']', '˽': '|', '˾': '|', '˿': '|',
    '「': '[', '」': ']', '『': '[', '』': ']', '【': '[', '】': ']',
    '〖': '[', '〗': ']', '〔': '(', '〕': ')', '〈': '<', '〉': '>',
    '《': '<', '》': '>', '•': '*', '·': '.', '‧': '.', '∙': '*', '◦': 'o',
    'Λ': 'A', 'λ': 'A', 'Α': 'A', 'α': 'a', 'Β': 'B', 'β': 'B',
    'Γ': 'G', 'γ': 'y', 'Δ': 'D', 'δ': 'd', 'Ε': 'E', 'ε': 'e',
    'Ζ': 'Z', 'ζ': 'z', 'Η': 'H', 'η': 'n', 'Θ': 'O', 'θ': 'o',
    'Ι': 'I', 'ι': 'i', 'Κ': 'K', 'κ': 'k', 'Μ': 'M', 'μ': 'u',
    'Ν': 'N', 'ν': 'v', 'Ξ': 'X', 'ξ': 'x', 'Ο': 'O', 'ο': 'o',
    'Π': 'P', 'π': 'n', 'Ρ': 'P', 'ρ': 'p', 'Σ': 'S', 'σ': 's',
    'Τ': 'T', 'τ': 't', 'Υ': 'Y', 'υ': 'u', 'Φ': 'F', 'φ': 'f',
    'Χ': 'X', 'χ': 'x', 'Ψ': 'Y', 'ψ': 'y', 'Ω': 'O', 'ω': 'w',
    'А': 'A', 'В': 'B', 'Е': 'E', 'К': 'K', 'М': 'M', 'Н': 'H',
    'О': 'O', 'Р': 'P', 'С': 'C', 'Т': 'T', 'У': 'Y', 'Х': 'X',
    'а': 'a', 'в': 'b', 'е': 'e', 'к': 'k', 'м': 'm', 'н': 'h',
    'о': 'o', 'р': 'p', 'с': 'c', 'т': 't', 'у': 'y', 'х': 'x',
}


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
    cmap = _CMAP[bold]
    out = []
    for ch in unicodedata.normalize("NFKD", text or ""):
        if unicodedata.category(ch) in _DROP_CATEGORIES:
            continue
        if ch in (" ", "\n", "\t") or ord(ch) in cmap:
            out.append(ch)
    cleaned = re.sub(r"\s+", " ", "".join(out)).strip()
    return cleaned or fallback


def _font_safe_name(name: str, bold: bool = True) -> str:
    text = re.sub(r"\s+", " ", (name or "").strip())
    if not text:
        return "Unknown"
    cmap = _CMAP[bold]
    out = []
    for ch in text:
        if ch == " ":
            out.append(ch)
            continue
        if ord(ch) in cmap:
            out.append(ch)
            continue
        sub = _NAME_LOOKALIKE.get(ch)
        if sub and ord(sub) in cmap:
            out.append(sub)
            continue
        for d in unicodedata.normalize("NFKD", ch):
            if d == " " or ord(d) in cmap:
                out.append(d)
    cleaned = re.sub(r"\s+", " ", "".join(out)).strip()
    return cleaned or "Unknown"


def _peer_color(user_id: int):
    return PEER_COLORS[abs(user_id) % len(PEER_COLORS)]


def _fit_512(img: Image.Image) -> Image.Image:
    w, h = img.size
    scale = MAX_SIDE / max(w, h)
    return img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


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
    try:
        photos = await ctx.bot.get_user_profile_photos(user.id, limit=1)
        if not photos.total_count:
            return None
        file_id = photos.photos[0][-1].file_id
        tgfile = await ctx.bot.get_file(file_id)
        return bytes(await tgfile.download_as_bytearray())
    except (TelegramError, OSError) as e:
        log.debug("profile photo fetch failed for %s: %s", user.id, e)
        return None


# ───────────────────── API path ─────────────────────

async def _build_quote_via_api(ctx, src_msg, sender) -> bytes | None:
    """Try each public quote API in turn. Returns .webp bytes or None."""
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
            "entities": [],
            "avatar": True,
            "from": {
                "id": sender.id,
                "name": sender.first_name or sender.full_name or "User",
                "photo": {"url": photo_url} if photo_url else {},
            },
            "text": text,
            "replyMessage": {},
        }],
    }

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url in QUOTE_APIS:
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if data and len(data) > 100:
                            log.info("quote built via %s (%d bytes)", url, len(data))
                            return data
                    log.warning("quote API %s returned %s", url, resp.status)
            except Exception as e:
                log.warning("quote API %s failed: %s", url, e)
    return None


# ───────────────────── local Pillow fallback ─────────────────────

async def _build_quote_via_pillow(ctx, src_msg, sender) -> bytes:
    """Local rendering — same visual style as the API, using DejaVu fonts."""
    W = 512
    outer_pad = 12
    avatar_size = 92
    gap = 14
    inner_pad = 22

    font_name = _load_font(True, 30)
    font_text = _load_font(False, 36)

    raw_text = src_msg.text or src_msg.caption or "[media]"
    text = _renderable(False, raw_text, "[unsupported characters]")
    sender_name = _font_safe_name(sender.full_name if sender else "", bold=True)

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

    # Avatar
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


# ───────────────────── shared command logic ─────────────────────

async def _build_quote_sticker(ctx, src_msg, sender) -> bytes:
    """Try APIs first; fall back to local Pillow if all of them fail."""
    data = await _build_quote_via_api(ctx, src_msg, sender)
    if data:
        return data
    log.warning("all quote APIs failed — falling back to local Pillow rendering")
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
        sticker_bytes = await _build_quote_sticker(ctx, src, sender)
    except Exception as e:
        log.error("quote generation failed: %s", e)
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
    await ctx.bot.send_sticker(chat.id, io.BytesIO(sticker_bytes), **kwargs)


async def q_cmd(update, ctx):
    await _quote_and_send(update, ctx, as_reply=False)


async def qr_cmd(update, ctx):
    await _quote_and_send(update, ctx, as_reply=True)


# ───────────────────────── .kang (unchanged) ─────────────────────────

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
