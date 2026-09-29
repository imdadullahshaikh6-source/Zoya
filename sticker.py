"""Sticker plugin:
.q / .qr  — render the replied-to message as a quote sticker
.kang     — steal a replied sticker/photo into the user's own auto-growing pack
            (the pack is created once per user and reused on every later .kang;
            if it fills up, a new part is started automatically).

Fonts are bundled in ./fonts (DejaVu Sans) so rendering never depends on
whatever fonts happen to be installed on the server — DejaVu covers Latin,
Cyrillic, Greek, and the IPA/phonetic "small caps" block that most fancy
Telegram-name generators use, so stylised display names render properly
instead of showing missing-glyph boxes."""
import io
import os
import re
import unicodedata

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont
from telegram import InputSticker
from telegram.error import TelegramError

import database as dbase
from common import T, dual_command, esc, say

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

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
FONT_REGULAR = os.path.join(FONT_DIR, "DejaVuSans.ttf")
FONT_BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")


def _load_font(bold: bool, size: int):
    path = FONT_BOLD if bold else FONT_REGULAR
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


# Real glyph coverage, read straight from each font's cmap table — checking
# via rendered-mask bounding boxes is unreliable because a missing-glyph
# "tofu" box also has a non-empty bbox, so that trick can't tell a real
# character from a placeholder box. Loaded once at import time.
def _cmap_of(path):
    try:
        return set(TTFont(path, fontNumber=0, lazy=True).getBestCmap())
    except Exception:
        return set()


_CMAP = {False: _cmap_of(FONT_REGULAR), True: _cmap_of(FONT_BOLD)}

# Zero-width/control/combining-mark characters render invisibly or misalign
# even when the font technically has a glyph for them, so they're dropped too.
_DROP_CATEGORIES = {"Cf", "Cc", "Co", "Cs", "Mn", "Me"}


def _renderable(bold: bool, text: str, fallback: str = "") -> str:
    # NFKD first: folds "fancy" letters built from compatibility blocks (bold/
    # italic/fullwidth Unicode math letters, e.g. "𝙕𝙤𝙮𝙖") back to plain ASCII,
    # while leaving true decorative marks (overlines, carets, ...) untouched.
    cmap = _CMAP[bold]
    out = []
    for ch in unicodedata.normalize("NFKD", text or ""):
        if unicodedata.category(ch) in _DROP_CATEGORIES:
            continue
        if ch in (" ", "\n", "\t") or ord(ch) in cmap:
            out.append(ch)
    cleaned = re.sub(r"\s+", " ", "".join(out)).strip()
    return cleaned or fallback


def _color_for(seed: int):
    palette = [(230, 126, 34), (155, 89, 182), (52, 152, 219), (231, 76, 60), (26, 188, 156), (241, 196, 15)]
    return palette[seed % len(palette)]


def _initials(name: str) -> str:
    words = re.findall(r"[A-Za-z]+", name or "")
    if not words:
        return "?"
    return (words[0][0] + (words[1][0] if len(words) > 1 else "")).upper()


def _fit_512(img: Image.Image) -> Image.Image:
    """Telegram static stickers need the longest side to be exactly 512px."""
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
    return lines[:8] or [""]


async def _avatar(ctx, user, size=100) -> Image.Image:
    try:
        photos = await ctx.bot.get_user_profile_photos(user.id, limit=1)
        if photos.photos:
            tgfile = await ctx.bot.get_file(photos.photos[0][-1].file_id)
            raw = await tgfile.download_as_bytearray()
            im = Image.open(io.BytesIO(bytes(raw))).convert("RGBA").resize((size, size))
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
            im.putalpha(mask)
            return im
    except (TelegramError, OSError):
        pass
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((0, 0, size, size), fill=_color_for(user.id))
    f = _load_font(True, size // 2)
    initials = _initials(_renderable(True, user.full_name, "?"))
    bbox = d.textbbox((0, 0), initials, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]), initials, font=f, fill=(255, 255, 255))
    return im


async def build_quote_sticker(ctx, src_msg, sender) -> io.BytesIO:
    W = 512  # design at the final sticker width so text never gets shrunk afterwards
    outer_pad = 20
    avatar_size = 100
    gap = 14
    pad = 24

    font_name = _load_font(True, 34)
    font_text = _load_font(False, 40)

    name = _renderable(True, sender.full_name, "Someone")[:28]
    raw_text = src_msg.text or src_msg.caption or "[media]"
    text = _renderable(False, raw_text, "[unsupported characters]")

    bubble_x = outer_pad + avatar_size + gap
    bubble_w = W - bubble_x - outer_pad
    text_w = bubble_w - pad * 2

    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines = _wrap(probe, text, font_text, text_w)
    line_h = font_text.getbbox("Ag")[3] + 16
    name_h = font_name.getbbox("Ag")[3] + 18
    body_h = line_h * len(lines)
    bubble_h = name_h + body_h + pad * 2
    H = max(avatar_size, bubble_h) + outer_pad * 2

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    by = (H - bubble_h) // 2
    draw.rounded_rectangle((bubble_x, by, bubble_x + bubble_w, by + bubble_h), radius=28, fill=(24, 37, 51, 240))

    draw.text((bubble_x + pad, by + pad - 2), name, font=font_name, fill=_color_for(sender.id))
    ty = by + pad + name_h
    for ln in lines:
        draw.text((bubble_x + pad, ty), ln, font=font_text, fill=(255, 255, 255))
        ty += line_h

    avatar = await _avatar(ctx, sender, avatar_size)
    img.paste(avatar, (outer_pad, (H - avatar_size) // 2), avatar)

    img = _fit_512(img)
    out = io.BytesIO()
    out.name = "quote.webp"
    img.save(out, "WEBP")
    out.seek(0)
    return out


async def _quote_and_send(update, ctx, as_reply: bool):
    msg, chat = update.effective_message, update.effective_chat
    src = msg.reply_to_message
    if not src:
        await say(ctx, chat.id, T("reply to a message with /q to turn it into a sticker."), reply_to=msg.message_id)
        return
    sender = src.from_user or msg.from_user
    try:
        out = await build_quote_sticker(ctx, src, sender)
    except Exception as e:  # image generation is best-effort, never crash the bot
        await say(ctx, chat.id, T("couldn't build that sticker:") + f" {esc(e)}", reply_to=msg.message_id)
        return
    kwargs = {"reply_to_message_id": src.message_id} if as_reply else {}
    await ctx.bot.send_sticker(chat.id, out, **kwargs)


async def q_cmd(update, ctx):
    await _quote_and_send(update, ctx, as_reply=False)


async def qr_cmd(update, ctx):
    await _quote_and_send(update, ctx, as_reply=True)


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
                          
