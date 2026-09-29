"""Sticker plugin:
.q / .qr  — render the replied-to message as a quote sticker
.kang     — steal a replied sticker/photo into the user's own auto-growing pack
            (the pack is created once per user and reused on every later .kang;
            if it fills up, a new part is created automatically)."""
import io
import re
import unicodedata

from PIL import Image, ImageDraw, ImageFont
from telegram import InputSticker
from telegram.error import TelegramError

import database as dbase
from common import T, dual_command, esc, say

# Fancy display names often use decorative Unicode (math-style bold letters,
# overlines, carets, fullwidth letters...) that most fonts can't render, which
# shows up as tofu boxes. NFKD-normalizing folds styled letters like "𝙕𝙤𝙮𝙖"
# back to plain "Zoya", and we then drop anything still outside a safe set —
# so a name is either shown cleanly or trimmed, never boxes.
_SAFE_NAME_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 "
    ".,!?'\"-_()&@#$%*+/:;~"
)



def _safe_name(name: str) -> str:
    """Preserve stylish Unicode names and remove invisible controls."""
    name = unicodedata.normalize("NFC", name or "")

    cleaned = "".join(
        ch for ch in name
        if unicodedata.category(ch) not in ("Cc", "Cs", "Co")
        and ch not in "\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
    )

    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    return cleaned[:80] or "Someone"
            

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


def _color_for(seed: int):
    palette = [(230, 126, 34), (155, 89, 182), (52, 152, 219), (231, 76, 60), (26, 188, 156), (241, 196, 15)]
    return palette[seed % len(palette)]


def _initials(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()


def _quote_font(size: int, bold: bool = False):
    """Load a proper scalable font for quote stickers."""
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",

        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        if bold else
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",

        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"
        if bold else
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]

    for path in paths:
        try:
            return ImageFont.truetype(path, size=size)
        except (OSError, ValueError):
            continue

    return ImageFont.load_default()
            


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


async def _avatar(ctx, user, size=84) -> Image.Image:
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
    try:
        f = ImageFont.load_default(size=size // 2)
    except TypeError:
        f = ImageFont.load_default()
    initials = _initials(_safe_name(user.full_name))
    bbox = d.textbbox((0, 0), initials, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]), initials, font=f, fill=(255, 255, 255))
    return im


async def build_quote_sticker(ctx, src_msg, sender) -> io.BytesIO:
    name = _safe_name(sender.full_name)

    text = src_msg.text or src_msg.caption or "[Media]"

    # ---------- CANVAS ----------
    W, H = 512, 512

    img = Image.new(
        "RGBA",
        (W, H),
        (0, 0, 0, 0)
    )

    draw = ImageDraw.Draw(img)

    # ---------- COLORS ----------
    accent = _color_for(sender.id)
    white = (255, 255, 255, 255)

    bubble_color = (24, 34, 50, 248)
    border_color = (80, 100, 130, 210)

    # ---------- BUBBLE ----------
    bx, by = 108, 72
    bw, bh = 382, 368
    radius = 32

    # Soft shadow
    draw.rounded_rectangle(
        (
            bx + 5,
            by + 8,
            bx + bw + 5,
            by + bh + 8
        ),
        radius=radius,
        fill=(0, 0, 0, 90)
    )

    # Main bubble
    draw.rounded_rectangle(
        (bx, by, bx + bw, by + bh),
        radius=radius,
        fill=bubble_color,
        outline=border_color,
        width=2
    )

    # Colored accent line
    draw.rounded_rectangle(
        (bx + 2, by + 25, bx + 9, by + bh - 25),
        radius=5,
        fill=accent
    )

    # ---------- AVATAR ----------
    avatar_size = 92

    avatar = await _avatar(
        ctx,
        sender,
        avatar_size
    )

    ax, ay = 22, 98

    # Avatar border
    draw.ellipse(
        (
            ax - 4,
            ay - 4,
            ax + avatar_size + 4,
            ay + avatar_size + 4
        ),
        fill=white
    )

    img.paste(
        avatar,
        (ax, ay),
        avatar
    )

    draw = ImageDraw.Draw(img)

    # ---------- USERNAME ----------
    name_x = bx + 30
    name_y = by + 29

    name_font_size = 27
    max_name_width = bw - 58

    name_font = _quote_font(
        name_font_size,
        bold=True
    )

    # Shrink only if username is too long.
    while (
        draw.textlength(name, font=name_font) > max_name_width
        and name_font_size > 15
    ):
        name_font_size -= 1

        name_font = _quote_font(
            name_font_size,
            bold=True
        )

    # Shadow behind name
    draw.text(
        (name_x + 1, name_y + 2),
        name,
        font=name_font,
        fill=(0, 0, 0, 150)
    )

    # Actual username
    draw.text(
        (name_x, name_y),
        name,
        font=name_font,
        fill=accent
    )

    # ---------- DIVIDER ----------
    divider_y = by + 84

    draw.line(
        (
            bx + 28,
            divider_y,
            bx + bw - 28,
            divider_y
        ),
        fill=(100, 120, 150, 150),
        width=2
    )

    # ---------- MESSAGE TEXT ----------
    text_x = bx + 29
    text_y = divider_y + 24

    text_width = bw - 58
    available_height = 218

    font_size = 28

    while font_size >= 16:
        font_text = _quote_font(
            font_size,
            bold=False
        )

        lines = _wrap(
    draw,
    text,
    font_text,
    text_width
)[:6]

        bbox = font_text.getbbox("Ag")
        line_height = bbox[3] - bbox[1] + 12

        total_height = len(lines) * line_height

        if total_height <= available_height:
            break

        font_size -= 1

    # Render each line
    for line in lines:
        draw.text(
            (text_x, text_y),
            line,
            font=font_text,
            fill=white
        )

        text_y += line_height

    # ---------- FOOTER ----------
    footer_font = _quote_font(15)

    draw.text(
        (
            bx + 29,
            by + bh - 38
        ),
        "ZOYA  •  QUOTE",
        font=footer_font,
        fill=(175, 190, 210, 255)
    )

    # ---------- EXPORT ----------
    # Keep original Telegram sticker resizing logic.
    img = _fit_512(img)

    out = io.BytesIO()
    out.name = "quote.webp"

    img.save(
        out,
        "WEBP",
        quality=95,
        method=6
    )

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
            
