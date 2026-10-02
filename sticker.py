"""Sticker plugin:
.q / .qr  — render the replied-to message as a Telegram-style quote sticker
.kang     — steal a replied sticker/photo into the user's own auto-growing pack

The quote sticker replicates a Telegram purple message bubble: rounded purple
box, sender's display name in bold at the top, message body below. The name
is the sender's full_name (never @username)."""
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

# Telegram dark-theme outgoing bubble — the purple from your screenshot.
BUBBLE_BG = (135, 116, 225, 255)
NAME_COLOR = (255, 255, 255)
TEXT_COLOR = (255, 255, 255)

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
FONT_REGULAR = os.path.join(FONT_DIR, "DejaVuSans.ttf")
FONT_BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")

# Lookalike map — substitutes decorative glyphs DejaVu can't draw, so the name
# never collapses into tofu boxes.
_NAME_LOOKALIKE = {
    '˹': '[', '˺': ']', '˻': '[', '˼': ']', '˽': '|', '˾': '|', '˿': '|',
    '「': '[', '」': ']', '『': '[', '』': ']',
    '【': '[', '】': ']', '〖': '[', '〗': ']',
    '〔': '(', '〕': ')', '〈': '<', '〉': '>', '《': '<', '》': '>',
    '•': '*', '·': '.', '‧': '.', '∙': '*', '◦': 'o',
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


async def build_quote_sticker(ctx, src_msg, sender) -> io.BytesIO:
    """Purple Telegram-style message bubble with the sender's name on top and
    the message body underneath. No avatar, no time, no tick — just like the
    screenshot the user provided."""
    W = 512
    outer_pad = 14
    inner_pad = 26

    font_name = _load_font(True, 34)
    font_text = _load_font(False, 38)

    raw_text = src_msg.text or src_msg.caption or "[media]"
    text = _renderable(False, raw_text, "[unsupported characters]")
    sender_name = _font_safe_name(sender.full_name if sender else "", bold=True)
    print(f"[sticker] name: {sender.full_name!r} -> {sender_name!r}")

    text_w = W - outer_pad * 2 - inner_pad * 2
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    name_lines = _wrap(probe, sender_name, font_name, text_w)
    name_line_h = font_name.getbbox("Ag")[3] + 8

    text_lines = _wrap(probe, text, font_text, text_w)
    text_line_h = font_text.getbbox("Ag")[3] + 14

    gap_name_text = 14
    body_h = (
        name_line_h * len(name_lines)
        + gap_name_text
        + text_line_h * len(text_lines)
    )
    bubble_h = body_h + inner_pad * 2
    H = bubble_h + outer_pad * 2

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # The purple message bubble
    draw.rounded_rectangle(
        (outer_pad, outer_pad, W - outer_pad, H - outer_pad),
        radius=30, fill=BUBBLE_BG,
    )

    ty = outer_pad + inner_pad
    # Name — bold white at the top
    for ln in name_lines:
        draw.text((outer_pad + inner_pad, ty), ln, font=font_name, fill=NAME_COLOR)
        ty += name_line_h
    ty += gap_name_text
    # Message body — regular white below
    for ln in text_lines:
        draw.text((outer_pad + inner_pad, ty), ln, font=font_text, fill=TEXT_COLOR)
        ty += text_line_h

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
    except Exception as e:
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
