"""Fun plugin: .waifu (random waifu pic) and .couple (ships two group members,
with both profile photos composited into one image)."""
import io
import random

import httpx
from PIL import Image, ImageDraw
from telegram import ReplyParameters
from telegram.constants import ChatType, ParseMode

import database as dbase
import sticker  # reuse its bundled-font + avatar-fetch helpers
from common import T, dual_command, esc, mention, q, say

HELP_TXT = (
    "<b>✦ fun</b>\n\n"
    "/waifu (or .waifu) — get a random waifu picture\n"
    "/couple (or .couple) — ships two random members of the group, with both "
    "their profile photos, for today"
)
COMMANDS = [("waifu", "Get a random waifu"), ("couple", "Ship two random members")]

# Tried in order; if the first API is down or rate-limited, the next is used.
WAIFU_APIS = [
    ("https://api.waifu.pics/sfw/waifu", lambda j: j["url"]),
    ("https://nekos.best/api/v2/waifu", lambda j: j["results"][0]["url"]),
]


async def waifu_cmd(update, ctx):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    url = None
    async with httpx.AsyncClient(timeout=10) as client:
        for api, extract in WAIFU_APIS:
            try:
                r = await client.get(api)
                r.raise_for_status()
                url = extract(r.json())
                break
            except Exception:
                continue
    if not url:
        await say(ctx, chat.id, T("couldn't fetch a waifu right now, try again in a bit."), reply_to=msg.message_id)
        return
    cap = q(T("<b>{m}'s waifu for today ✨</b>", m=mention(user)))
    await ctx.bot.send_photo(
        chat.id, url, caption=cap, parse_mode=ParseMode.HTML,
        reply_parameters=ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True),
    )


class _FakeUser:
    """Minimal stand-in so we can reuse sticker._avatar() with just an id+name."""
    def __init__(self, uid, name):
        self.id = uid
        self.full_name = name


def _draw_heart(draw, cx, cy, w, color):
    r = w / 4
    draw.ellipse((cx - w / 2, cy - r, cx, cy + r), fill=color)
    draw.ellipse((cx, cy - r, cx + w / 2, cy + r), fill=color)
    draw.polygon([(cx - w / 2, cy), (cx + w / 2, cy), (cx, cy + w / 2)], fill=color)


async def _build_couple_image(ctx, a, b, pct) -> io.BytesIO:
    W, H = 720, 420
    img = Image.new("RGB", (W, H), (18, 22, 30))
    draw = ImageDraw.Draw(img)

    title_font = sticker._load_font(True, 40)
    name_font = sticker._load_font(True, 30)
    pct_font = sticker._load_font(True, 46)

    title = "Today's Couple"
    tb = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((W - (tb[2] - tb[0])) / 2, 24), title, font=title_font, fill=(255, 105, 180))

    avatar_size = 190
    ay = 110
    left_x, right_x = 70, W - 70 - avatar_size

    a_img = await sticker._avatar(ctx, a, avatar_size)
    b_img = await sticker._avatar(ctx, b, avatar_size)
    img.paste(a_img, (left_x, ay), a_img)
    img.paste(b_img, (right_x, ay), b_img)

    _draw_heart(draw, W // 2, ay + avatar_size // 2, 90, (231, 76, 60))

    a_name = sticker._renderable(True, a.full_name, "Someone")[:16]
    b_name = sticker._renderable(True, b.full_name, "Someone")[:16]
    for name, cx in ((a_name, left_x + avatar_size / 2), (b_name, right_x + avatar_size / 2)):
        nb = draw.textbbox((0, 0), name, font=name_font)
        draw.text((cx - (nb[2] - nb[0]) / 2, ay + avatar_size + 16), name, font=name_font, fill=(255, 255, 255))

    pct_text = f"{pct}% match"
    pb = draw.textbbox((0, 0), pct_text, font=pct_font)
    draw.text(((W - (pb[2] - pb[0])) / 2, ay + avatar_size + 74), pct_text, font=pct_font, fill=(46, 204, 113))

    out = io.BytesIO()
    out.name = "couple.png"
    img.save(out, "PNG")
    out.seek(0)
    return out


async def couple_cmd(update, ctx):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        await say(ctx, chat.id, T("this only works inside groups."))
        return
    members = await dbase.random_members(chat.id, 8)
    if len(members) < 2:
        await say(ctx, chat.id, T("not enough active members seen yet — chat a bit more first!"), reply_to=msg.message_id)
        return
    m1, m2 = random.sample(members, 2)
    a = _FakeUser(m1["user_id"], m1.get("name") or "Someone")
    b = _FakeUser(m2["user_id"], m2.get("name") or "Someone")
    pct = random.randint(40, 100)

    try:
        photo = await _build_couple_image(ctx, a, b, pct)
    except Exception as e:
        await say(ctx, chat.id, T("couldn't build that image:") + f" {esc(e)}", reply_to=msg.message_id)
        return

    cap = q(T("<b>💞 today's couple</b>\n{a} + {b}", a=esc(a.full_name), b=esc(b.full_name)))
    await ctx.bot.send_photo(
        chat.id, photo, caption=cap, parse_mode=ParseMode.HTML,
        reply_parameters=ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True),
    )


def register(app):
    dual_command(app, "waifu", waifu_cmd, group_only=False)
    dual_command(app, "couple", couple_cmd, group_only=False)
    
