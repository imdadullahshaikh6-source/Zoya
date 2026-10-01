"""Fun plugin: .waifu (find a waifu from the group) and .couple (ship two members)."""
import io
import random
import time

from PIL import Image, ImageDraw, ImageFilter
from telegram import ReplyParameters
from telegram.constants import ChatType, ParseMode

import database as dbase
import sticker
from common import T, dual_command, esc, mention, q, say

HELP_TXT = (
    "<b>✦ fun</b>\n\n"
    "/waifu (or .waifu) — find your waifu from this group (changes every 24h)\n"
    "/couple (or .couple) — ship two members of this group (changes every 24h)"
)
COMMANDS = [("waifu", "Find a waifu from this group"), ("couple", "Ship two members")]

WAIFU_24H = 24 * 3600
COUPLE_24H = 24 * 3600


# ───────── gender heuristic ─────────
_FEMALE_NAMES = {
    "priya", "sneha", "riya", "siya", "diya", "kiya", "mia", "ria", "tia", "nia",
    "zoya", "ananya", "aarohi", "aditi", "anika", "avni", "isha", "juhi", "kavya",
    "kiara", "mahira", "meera", "myra", "navya", "pari", "saanvi", "tara",
    "trisha", "vanya", "zara", "aisha", "fatima", "emma", "olivia", "sophia",
    "ava", "isabella", "amelia", "harper", "evelyn", "abigail", "emily",
    "elizabeth", "sofia", "ella", "scarlett", "grace", "chloe", "victoria",
    "riley", "aria", "lily", "aubrey", "zoey", "penelope", "layla", "nora",
    "camila", "hazel", "madison", "aurora", "bella", "skylar", "lucy",
    "paisley", "everly", "anna", "sarah", "maya", "luna", "stella", "violet",
    "willow", "jade", "ivy", "rose", "daisy", "poppy", "iris", "ruby", "pearl",
    "jasmine", "nisha", "pooja", "neha", "shruti", "swati", "anita", "sunita",
}
_MALE_NAMES = {
    "aarav", "advait", "aryan", "atharv", "ayaan", "dhruv", "harsh", "ishaan",
    "kabir", "krishna", "kunal", "laksh", "madhav", "neel", "om", "parth",
    "pranav", "reyansh", "rudra", "ryan", "shiv", "shourya", "vihaan", "vivaan",
    "yash", "john", "james", "robert", "michael", "william", "david", "richard",
    "joseph", "thomas", "charles", "christopher", "daniel", "matthew",
    "anthony", "donald", "mark", "paul", "steven", "andrew", "kenneth",
    "george", "joshua", "kevin", "brian", "edward", "ronald", "timothy",
    "jason", "jeffrey", "jacob", "gary", "nicholas", "eric", "jonathan",
    "stephen", "larry", "justin", "scott", "brandon", "benjamin", "samuel",
    "gregory", "frank", "alexander", "raymond", "patrick", "jack", "dennis",
    "jerry", "tyler", "aaron", "jose", "adam", "nathan", "henry", "zachary",
    "douglas", "peter", "kyle", "noah", "ethan", "jeremy", "walter",
    "christian", "keith", "roger", "terry", "gerald", "harold", "sean",
    "austin", "carl", "arthur", "lawrence", "dylan", "jesse", "jordan",
    "bryan", "amit", "rahul", "rohit", "suresh", "ramesh", "vikas", "vikram",
    "arjun", "karan", "manish", "naveen", "pankaj", "raj", "sanjay", "sunil",
    "vijay", "ajay", "anil", "ashok", "deepak", "gopal", "harish", "jatin",
    "kartik", "lokesh", "mahesh", "naresh", "omkar", "prakash", "rakesh",
    "sachin", "tarun", "umesh", "varun", "yogesh",
}


def _guess_gender(name: str) -> str:
    n = (name or "").lower().strip()
    if not n:
        return "unknown"
    first = n.split()[0]
    if first in _FEMALE_NAMES:
        return "female"
    if first in _MALE_NAMES:
        return "male"
    # heuristic by suffix
    if first.endswith(("a", "i", "ee", "ya", "ina", "isha", "ita", "ana", "ika")):
        return "female"
    if first.endswith(("dev", "ish", "it", "iv", "om", "on", "ay", "an", "ar", "av")):
        return "male"
    return "unknown"


class _FakeUser:
    """Minimal stand-in for sticker._avatar()."""
    def __init__(self, uid, name):
        self.id = uid
        self.full_name = name


# ───────── drawing helpers ─────────
def _vertical_gradient(w, h, top, bottom):
    grad = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / h
        r = int(top[0] * (1 - t) + bottom[0] * t)
        g = int(top[1] * (1 - t) + bottom[1] * t)
        b = int(top[2] * (1 - t) + bottom[2] * t)
        grad.putpixel((0, y), (r, g, b))
    return grad.resize((w, h))


def _rounded_avatar(img, size, radius):
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, size, size), radius=radius, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _draw_heart(draw, cx, cy, w, color):
    r = w / 4
    draw.ellipse((cx - w / 2, cy - r, cx, cy + r), fill=color)
    draw.ellipse((cx, cy - r, cx + w / 2, cy + r), fill=color)
    draw.polygon([(cx - w / 2, cy), (cx + w / 2, cy), (cx, cy + w / 2)], fill=color)


# ───────── db helpers ─────────
async def _get_waifu(chat_id, user_id):
    return await dbase.db.waifu.find_one({"chat_id": chat_id, "user_id": user_id})


async def _save_waifu(chat_id, user_id, target_id):
    await dbase.db.waifu.update_one(
        {"chat_id": chat_id, "user_id": user_id},
        {"$set": {"target_id": target_id, "assigned_at": time.time()}},
        upsert=True,
    )


async def _get_couple(chat_id):
    return await dbase.db.couples.find_one({"chat_id": chat_id})


async def _save_couple(chat_id, a_id, b_id):
    await dbase.db.couples.update_one(
        {"chat_id": chat_id},
        {"$set": {"user_a": a_id, "user_b": b_id, "assigned_at": time.time()}},
        upsert=True,
    )


# ───────── .waifu ─────────
async def waifu_cmd(update, ctx):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        await say(ctx, chat.id, T("this only works inside groups."))
        return

    target_id = None
    existing = await _get_waifu(chat.id, user.id)
    if existing and (time.time() - existing.get("assigned_at", 0)) < WAIFU_24H:
        # verify target still in group
        try:
            await chat.get_member(existing["target_id"])
            target_id = existing["target_id"]
        except Exception:
            target_id = None

    if not target_id:
        members = await dbase.random_members(chat.id, 30)
        members = [m for m in members if m.get("user_id") and m["user_id"] != user.id]
        if not members:
            await say(ctx, chat.id, T("not enough active members seen yet — chat a bit more first!"), reply_to=msg.message_id)
            return

        sender_gender = _guess_gender(user.first_name or "")
        candidates = []
        for m in members:
            g = _guess_gender(m.get("name") or "")
            if sender_gender == "male" and g == "female":
                candidates.append(m)
            elif sender_gender == "female" and g == "male":
                candidates.append(m)
        if not candidates:
            candidates = members
        target = random.choice(candidates)
        target_id = target["user_id"]
        await _save_waifu(chat.id, user.id, target_id)

    target_member = await dbase.db.members.find_one({"chat_id": chat.id, "user_id": target_id}) or {}
    target_name = target_member.get("name", "Someone")

    reply_params = ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True)
    cap = q(T("<b>💖 {name} is your waifu for today!</b>", name=esc(target_name)))

    # try Telegram profile photo first
    file_id = None
    try:
        photos = await ctx.bot.get_user_profile_photos(target_id, limit=1)
        if photos.total_count > 0:
            file_id = photos.photos[0][-1].file_id
    except Exception:
        pass

    if file_id:
        await ctx.bot.send_photo(chat.id, file_id, caption=cap, parse_mode=ParseMode.HTML, reply_parameters=reply_params)
    else:
        # fallback: draw avatar via sticker helper
        target_user = _FakeUser(target_id, target_name)
        try:
            img = await sticker._avatar(ctx, target_user, 512)
            out = io.BytesIO()
            out.name = "waifu.png"
            img.save(out, "PNG")
            out.seek(0)
            await ctx.bot.send_photo(chat.id, out, caption=cap, parse_mode=ParseMode.HTML, reply_parameters=reply_params)
        except Exception as e:
            await say(ctx, chat.id, T("couldn't fetch that waifu's photo:") + f" {esc(e)}", reply_to=msg.message_id)


# ───────── .couple ─────────
async def _build_couple_image(ctx, a, b, pct) -> io.BytesIO:
    W, H = 800, 500
    img = _vertical_gradient(W, H, (35, 15, 60), (10, 20, 55))
    draw = ImageDraw.Draw(img)

    title_font = sticker._load_font(True, 48)
    name_font = sticker._load_font(True, 32)
    pct_font = sticker._load_font(True, 52)

    title = "Today's Couple"
    tb = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((W - (tb[2] - tb[0])) / 2, 30), title, font=title_font, fill=(255, 105, 180))

    avatar_size = 220
    ay = 150
    left_x = 90
    right_x = W - 90 - avatar_size

    a_img = await sticker._avatar(ctx, a, avatar_size)
    b_img = await sticker._avatar(ctx, b, avatar_size)

    a_round = _rounded_avatar(a_img, avatar_size, 40)
    b_round = _rounded_avatar(b_img, avatar_size, 40)

    img.paste(a_round, (left_x, ay), a_round)
    img.paste(b_round, (right_x, ay), b_round)

    cx, cy = W // 2, ay + avatar_size // 2
    # glow
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    _draw_heart(gdraw, cx, cy, 150, (255, 80, 120, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(18))
    img.paste(glow, (0, 0), glow)
    # solid heart
    _draw_heart(draw, cx, cy, 100, (255, 55, 100))

    a_name = sticker._renderable(True, a.full_name, "Someone")[:16]
    b_name = sticker._renderable(True, b.full_name, "Someone")[:16]
    for name, x in ((a_name, left_x + avatar_size / 2), (b_name, right_x + avatar_size / 2)):
        nb = draw.textbbox((0, 0), name, font=name_font)
        draw.text((x - (nb[2] - nb[0]) / 2, ay + avatar_size + 20), name, font=name_font, fill=(255, 255, 255))

    pct_text = f"{pct}% match"
    pb = draw.textbbox((0, 0), pct_text, font=pct_font)
    draw.text(((W - (pb[2] - pb[0])) / 2, ay + avatar_size + 90), pct_text, font=pct_font, fill=(80, 220, 120))

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

    a_id = b_id = None
    existing = await _get_couple(chat.id)
    if existing and (time.time() - existing.get("assigned_at", 0)) < COUPLE_24H:
        try:
            await chat.get_member(existing["user_a"])
            await chat.get_member(existing["user_b"])
            a_id, b_id = existing["user_a"], existing["user_b"]
        except Exception:
            a_id = b_id = None

    if not a_id or not b_id:
        members = await dbase.random_members(chat.id, 30)
        if len(members) < 2:
            await say(ctx, chat.id, T("not enough active members seen yet — chat a bit more first!"), reply_to=msg.message_id)
            return
        males = [m for m in members if _guess_gender(m.get("name") or "") == "male"]
        females = [m for m in members if _guess_gender(m.get("name") or "") == "female"]
        if males and females:
            a = random.choice(males)
            b = random.choice(females)
        else:
            a, b = random.sample(members, 2)
        a_id, b_id = a["user_id"], b["user_id"]
        await _save_couple(chat.id, a_id, b_id)

    a_member = await dbase.db.members.find_one({"chat_id": chat.id, "user_id": a_id}) or {}
    b_member = await dbase.db.members.find_one({"chat_id": chat.id, "user_id": b_id}) or {}
    a_name = a_member.get("name", "Someone")
    b_name = b_member.get("name", "Someone")

    a = _FakeUser(a_id, a_name)
    b = _FakeUser(b_id, b_name)
    pct = random.randint(40, 100)

    try:
        photo = await _build_couple_image(ctx, a, b, pct)
    except Exception as e:
        await say(ctx, chat.id, T("couldn't build that image:") + f" {esc(e)}", reply_to=msg.message_id)
        return

    cap = q(T("<b>💞 today's couple</b>\n{a} + {b}", a=esc(a_name), b=esc(b_name)))
    await ctx.bot.send_photo(
        chat.id, photo, caption=cap, parse_mode=ParseMode.HTML,
        reply_parameters=ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True),
    )


def register(app):
    dual_command(app, "waifu", waifu_cmd, group_only=False)
    dual_command(app, "couple", couple_cmd, group_only=False)
