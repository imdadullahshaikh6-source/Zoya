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


def _clean_text(text: str) -> str:
    """Unicode normalizer jo stylish fonts ko clean readable banata hai bina boxes ke."""
    if not text:
        return ""
    # Mathematical styled characters (bold, script, monospace etc.) ko base letters me convert karta hai
    normalized = unicodedata.normalize("NFKD", text)
    # Control / invisible characters ko remove karta hai
    cleaned = "".join(
        ch for ch in normalized
        if unicodedata.category(ch) not in ("Cc", "Cs", "Cf")
    )
    return cleaned.strip()


def _safe_name(name: str) -> str:
    cleaned = _clean_text(name)
    return cleaned[:35] or "User"


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
    palette = [
        (255, 110, 180),  # Pinkish / Magenta
        (255, 140, 100),  # Coral
        (130, 200, 255),  # Sky Blue
        (170, 130, 255),  # Violet
        (90, 230, 160),   # Light Green
        (255, 215, 100),  # Warm Gold
    ]
    return palette[seed % len(palette)]


def _initials(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()


def _get_font(size: int, bold: bool = False):
    """Broad font support with standard linux/android/windows fallbacks."""
    paths = [
        # Linux standard
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if bold else "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        # Windows
        "C:\\Windows\\Fonts\\segoeuib.ttf" if bold else "C:\\Windows\\Fonts\\segoeui.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf" if bold else "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size=size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _fit_512(img: Image.Image) -> Image.Image:
    w, h = img.size
    scale = MAX_SIDE / max(w, h)
    return img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_w: int):
    paragraphs = text.split("\n")
    lines = []
    for para in paragraphs:
        if not para.strip():
            lines.append("")
            continue
        words = para.split(" ")
        cur = ""
        for w in words:
            trial = (cur + " " + w).strip() if cur else w
            if draw.textlength(trial, font=font) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                if draw.textlength(w, font=font) > max_w:
                    sub = ""
                    for ch in w:
                        if draw.textlength(sub + ch, font=font) <= max_w:
                            sub += ch
                        else:
                            lines.append(sub)
                            sub = ch
                    cur = sub
                else:
                    cur = w
        if cur:
            lines.append(cur)
    return lines[:10] or [""]


async def _avatar(ctx, user, size: int = 140) -> Image.Image:
    try:
        photos = await ctx.bot.get_user_profile_photos(user.id, limit=1)
        if photos.photos:
            tgfile = await ctx.bot.get_file(photos.photos[0][-1].file_id)
            raw = await tgfile.download_as_bytearray()
            im = Image.open(io.BytesIO(bytes(raw))).convert("RGBA").resize((size, size), Image.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
            im.putalpha(mask)
            return im
    except Exception:
        pass

    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((0, 0, size, size), fill=_color_for(user.id))
    f = _get_font(int(size * 0.42), bold=True)
    initials = _initials(_safe_name(user.full_name))
    bbox = d.textbbox((0, 0), initials, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]), initials, font=f, fill=(255, 255, 255, 255))
    return im


async def build_quote_sticker(ctx, src_msg, sender) -> io.BytesIO:
    name = _safe_name(sender.full_name)
    raw_text = src_msg.text or src_msg.caption or "[Media]"
    text = _clean_text(raw_text)

    # Dummy draw measurement
    dummy = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    d = ImageDraw.Draw(dummy)

    # Clean fonts
    name_font = _get_font(36, bold=True)
    text_font = _get_font(38, bold=False)
    accent = _color_for(sender.id)

    max_w = 620
    lines = _wrap_text(d, text, text_font, max_w)

    name_w = d.textlength(name, font=name_font)
    text_w = max((d.textlength(l, font=text_font) for l in lines), default=160)

    # Padding and proportions
    pad_left = 68
    pad_right = 50
    pad_top = 28
    pad_bottom = 32

    content_w = max(name_w, text_w, 200)
    bubble_w = int(content_w + pad_left + pad_right)

    name_bbox = name_font.getbbox("Ag")
    name_h = name_bbox[3] - name_bbox[1]

    sample_bbox = text_font.getbbox("Ag")
    line_h = (sample_bbox[3] - sample_bbox[1]) + 18
    body_h = len(lines) * line_h

    bubble_h = pad_top + name_h + 16 + body_h + pad_bottom

    avatar_size = 110
    # Minimum bubble height matches avatar proportions
    bubble_h = max(bubble_h, avatar_size + 14)

    # Canvas dimensions
    canvas_w = bubble_w + 80
    canvas_h = bubble_h + 50

    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Bubble coordinates (Avatar overlaps left-top side like example)
    bx = 45
    by = 22
    radius = min(48, bubble_h // 2)

    # Soft Shadow
    draw.rounded_rectangle(
        (bx + 4, by + 6, bx + bubble_w + 4, by + bubble_h + 6),
        radius=radius,
        fill=(0, 0, 0, 75)
    )

    # Bubble Body (Dark Telegram Theme Style)
    bubble_bg = (28, 22, 38, 245)
    draw.rounded_rectangle(
        (bx, by, bx + bubble_w, by + bubble_h),
        radius=radius,
        fill=bubble_bg
    )

    # Overlapping Avatar on Top-Left
    avatar = await _avatar(ctx, sender, avatar_size)
    ax = bx - 35
    ay = by - 8
    img.paste(avatar, (ax, ay), avatar)

    draw = ImageDraw.Draw(img)

    # Sender Name
    tx = bx + pad_left
    ty = by + pad_top
    draw.text((tx, ty), name, font=name_font, fill=accent)

    # Message Lines
    curr_y = ty + name_h + 16
    for line in lines:
        draw.text((tx, curr_y), line, font=text_font, fill=(255, 255, 255, 255))
        curr_y += line_h

    # Resize to standard sticker size
    img = _fit_512(img)

    out = io.BytesIO()
    out.name = "quote.webp"
    img.save(out, "WEBP", quality=95, method=6)
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
    except Exception as e:
        await say(ctx, chat.id, T("couldn't build that sticker:") + f" {esc(e)}", reply_to=msg.message_id)
        return
    kwargs = {"reply_to_message_id": src.message_id} if as_reply else {}
    await ctx.bot.send_sticker(chat.id, out, **kwargs)


async def q_cmd(update, ctx):
    await _quote_and_send(update, ctx, as_reply=False)


async def qr_cmd(update, ctx):
    await _quote_and_send(update, ctx, as_reply=True)


# =====================================================================
# KANG COMMANDS (UNTOUCHED)
# =====================================================================

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
    
