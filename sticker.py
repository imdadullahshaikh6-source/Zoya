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


def _safe_name(name: str) -> str:
    """Preserve stylish Unicode names and remove invisible controls."""
    name = unicodedata.normalize("NFC", name or "")
    cleaned = "".join(
        ch for ch in name
        if unicodedata.category(ch) not in ("Cc", "Cs", "Co")
        and ch not in "\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:40] or "User"


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
        (255, 136, 94),   # Orange-Red
        (225, 112, 85),   # Coral
        (253, 121, 168),  # Pink
        (108, 92, 231),   # Violet
        (0, 206, 201),    # Cyan
        (9, 132, 227),    # Blue
        (0, 184, 148),    # Green
        (254, 202, 87)    # Gold
    ]
    return palette[seed % len(palette)]


def _initials(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()


def _get_font(size: int, bold: bool = False):
    """Load a proper font with robust fallbacks across environments."""
    paths = [
        # Linux standard fonts
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if bold else "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        # Windows standard fonts
        "C:\\Windows\\Fonts\\arialbd.ttf" if bold else "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\segoeui.ttf",
    ]
    for path in paths:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _fit_512(img: Image.Image) -> Image.Image:
    """Telegram static stickers require the longest side to be exactly 512px."""
    w, h = img.size
    scale = MAX_SIDE / max(w, h)
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    return img.resize((new_w, new_h), Image.LANCZOS)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_w: int):
    """Wrap message text properly into lines."""
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
                # Handle single word exceeding max width
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
    return lines[:12] or [""]


async def _avatar(ctx, user, size: int = 100) -> Image.Image:
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

    # Fallback to initials circle
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
    text = src_msg.text or src_msg.caption or "[Media]"

    # Setup dummy canvas for measuring
    dummy_img = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    measure_draw = ImageDraw.Draw(dummy_img)

    # Styling & Fonts
    name_font = _get_font(34, bold=True)
    text_font = _get_font(30, bold=False)
    accent_color = _color_for(sender.id)

    # Word wrap text
    max_text_w = 560
    lines = _wrap_text(measure_draw, text, text_font, max_text_w)

    # Calculate dimensions
    name_w = measure_draw.textlength(name, font=name_font)
    text_w = max((measure_draw.textlength(l, font=text_font) for l in lines), default=120)

    content_w = max(name_w, text_w, 140)
    padding_x = 34
    padding_y = 26
    bubble_w = int(content_w + (padding_x * 2) + 20)

    # Calculate text lines height
    sample_bbox = text_font.getbbox("Ag")
    line_h = (sample_bbox[3] - sample_bbox[1]) + 14
    body_h = len(lines) * line_h

    name_bbox = name_font.getbbox("Ag")
    name_h = name_bbox[3] - name_bbox[1]

    bubble_h = padding_y + name_h + 16 + body_h + padding_y

    avatar_size = 100
    bubble_h = max(bubble_h, avatar_size + 20)

    # Canvas spacing
    canvas_w = avatar_size + 26 + bubble_w + 30
    canvas_h = bubble_h + 40

    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Bubble Position
    bx = avatar_size + 22
    by = 16

    # Shadow
    draw.rounded_rectangle(
        (bx + 4, by + 6, bx + bubble_w + 4, by + bubble_h + 6),
        radius=26,
        fill=(0, 0, 0, 70)
    )

    # Bubble Background
    bubble_bg = (24, 30, 42, 245)
    border_color = (65, 82, 105, 180)
    draw.rounded_rectangle(
        (bx, by, bx + bubble_w, by + bubble_h),
        radius=26,
        fill=bubble_bg,
        outline=border_color,
        width=2
    )

    # Accent Strip inside bubble
    draw.rounded_rectangle(
        (bx + 6, by + 18, bx + 12, by + bubble_h - 18),
        radius=3,
        fill=accent_color
    )

    # Avatar
    avatar = await _avatar(ctx, sender, avatar_size)
    ay = by + bubble_h - avatar_size  # Aligned towards the bottom edge like TG
    img.paste(avatar, (10, ay), avatar)

    draw = ImageDraw.Draw(img)

    # Sender Name
    tx = bx + padding_x + 6
    ty = by + padding_y - 4
    draw.text((tx, ty), name, font=name_font, fill=accent_color)

    # Message Text
    curr_y = ty + name_h + 16
    for line in lines:
        draw.text((tx, curr_y), line, font=text_font, fill=(255, 255, 255, 255))
        curr_y += line_h

    # Resize to exact Telegram 512px sticker rule
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
                
