"""Fun plugin: .waifu (find a waifu from the group) and .couple (ship two members)."""
import io
import logging
import random
import time

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from telegram import ReplyParameters
from telegram.constants import ChatType, ParseMode

import database as dbase
import sticker
from common import T, dual_command, esc, mention, q, say

log = logging.getLogger("fun")

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
    if first.endswith(("a", "i", "ee", "ya", "ina", "isha", "ita", "ana", "ika")):
        return "female"
    if first.endswith(("dev", "ish", "it", "iv", "om", "on", "ay", "an", "ar", "av")):
        return "male"
    return "unknown"


def _html_mention(user_id: int, name: str) -> str:
    """Build a clickable Telegram user mention."""
    safe_name = esc(name or "Someone")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


# ───────── profile photo fetcher ─────────
async def _fetch_profile_photo(ctx, user_id: int):
    """Fetch user's real Telegram profile photo as PIL Image. Returns None if none."""
    try:
        photos = await ctx.bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count == 0:
            return None
        file_id = photos.photos[0][-1].file_id
        tg_file = await ctx.bot.get_file(file_id)
        bio = io.BytesIO()
        await tg_file.download_to_memory(bio)
        bio.seek(0)
        return Image.open(bio).convert("RGBA")
    except Exception as e:
        log.warning("profile photo fetch failed for %s: %s", user_id, e)
        return None


def _placeholder_avatar(name: str, size: int) -> Image.Image:
    """Pretty fallback: colored circle + big initial."""
    import colorsys
    hue = hash(name or "x") % 360
    r, g, b = colorsys.hsv_to_rgb(hue / 360, 0.65, 0.85)
    color = (int(r * 255), int(g * 255), int(b * 255))
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, size, size), fill=color)
    initial = (name or "?").strip()[:1].upper() or "?"
    try:
        f = ImageFont.truetype("DejaVuSans-Bold.ttf", int(size * 0.5))
    except Exception:
        f = ImageFont.load_default()
    bbox = d.textbbox((0, 0), initial, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]), initial, font=f, fill=(255, 255, 255))
    return img


async def _get_avatar(ctx, user_id: int, name: str, size: int) -> Image.Image:
    """Real photo if available, else pretty placeholder."""
    img = await _fetch_profile_photo(ctx, user_id)
    if img is None:
        return _placeholder_avatar(name, size)
    w, h = img.size
    side = min(w, h)
    img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
    return img.resize((size, size), Image.LANCZOS)


# ───────── db helpers ─────────
async def _get_waifu(chat_id, user_id):
    return await dbase.db.waifu.find_one({"chat_id": chat_id, "user_id": user_id})


async def _find_mutual_waifu(chat_id, target_id):
    """Find someone whose waifu is `target_id` (within 24h)."""
    cutoff = time.time() - WAIFU_24H
    return await dbase.db.waifu.find_one({
        "chat_id": chat_id,
        "target_id": target_id,
        "assigned_at": {"$gt": cutoff},
    })


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
    is_mutual = False

    # 1. User's existing waifu (within 24h)
    existing = await _get_waifu(chat.id, user.id)
    if existing and (time.time() - existing.get("assigned_at", 0)) < WAIFU_24H:
        try:
            await chat.get_member(existing["target_id"])
            target_id = existing["target_id"]
        except Exception:
            target_id = None

    # 2. Mutual check: someone already has THIS user as their waifu?
    if not target_id:
        mutual = await _find_mutual_waifu(chat.id, user.id)
        if mutual and mutual["user_id"] != user.id:
            try:
                await chat.get_member(mutual["user_id"])
                target_id = mutual["user_id"]
                is_mutual = True
                await _save_waifu(chat.id, user.id, target_id)
            except Exception:
                target_id = None

    # 3. Fresh assignment
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

    # ✅ BOTH mentions — the command sender AND the waifu
    sender_name = user.first_name or "Someone"
    sender_mention = _html_mention(user.id, sender_name)
    waifu_mention = _html_mention(target_id, target_name)

    if is_mutual:
        cap = q(T(
            "<b>💖 It's mutual!</b>\n{sender}, {waifu} is your waifu for today!",
            sender=sender_mention, waifu=waifu_mention,
        ))
    else:
        cap = q(T(
            "<b>💖 {sender}, {waifu} is your waifu for today!</b>",
            sender=sender_mention, waifu=waifu_mention,
        ))

    # Try direct file_id send (fastest) — spoiler
    try:
        photos = await ctx.bot.get_user_profile_photos(target_id, limit=1)
        if photos.total_count > 0:
            file_id = photos.photos[0][-1].file_id
            await ctx.bot.send_photo(chat.id, file_id, caption=cap,
                                     parse_mode=ParseMode.HTML, reply_parameters=reply_params,
                                     has_spoiler=True)
            return
    except Exception as e:
        log.warning("send via file_id failed: %s", e)

    # Fallback placeholder — spoiler
    img = await _get_avatar(ctx, target_id, target_name, 512)
    out = io.BytesIO()
    out.name = "waifu.png"
    img.save(out, "PNG")
    out.seek(0)
    await ctx.bot.send_photo(chat.id, out, caption=cap,
                             parse_mode=ParseMode.HTML, reply_parameters=reply_params,
                             has_spoiler=True)


# ───────── image builders ─────────
def _vertical_gradient(w, h, top, bottom):
    grad = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / h
        r = int(top[0] * (1 - t) + bottom[0] * t)
        g = int(top[1] * (1 - t) + bottom[1] * t)
        b = int(top[2] * (1 - t) + bottom[2] * t)
        grad.putpixel((0, y), (r, g, b))
    return grad.resize((w, h))


def _rounded(img, radius):
    size = img.size[0]
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


def _font(bold, size):
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _renderable(text: str) -> str:
    return "".join(c for c in (text or "") if c.isprintable())


async def _build_couple_image(ctx, a_id, a_name, b_id, b_name, pct) -> io.BytesIO:
    W, H = 820, 520
    img = _vertical_gradient(W, H, (35, 15, 60), (10, 20, 55)).convert("RGBA")
    draw = ImageDraw.Draw(img)

    title_font = _font(True, 46)
    name_font = _font(True, 30)
    pct_font = _font(True, 46)

    title = "Today's Couple"
    tb = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((W - (tb[2] - tb[0])) / 2, 30), title, font=title_font, fill=(255, 105, 180))

    avatar_size = 220
    ay = 160
    left_x = 90
    right_x = W - 90 - avatar_size

    a_img = await _get_avatar(ctx, a_id, a_name, avatar_size)
    b_img = await _get_avatar(ctx, b_id, b_name, avatar_size)
    a_round = _rounded(a_img, 40)
    b_round = _rounded(b_img, 40)
    img.paste(a_round, (left_x, ay), a_round)
    img.paste(b_round, (right_x, ay), b_round)

    cx, cy = W // 2, ay + avatar_size // 2
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    _draw_heart(gdraw, cx, cy, 160, (255, 80, 120, 100))
    glow = glow.filter(ImageFilter.GaussianBlur(20))
    img.paste(glow, (0, 0), glow)
    _draw_heart(draw, cx, cy, 95, (255, 55, 100))

    a_disp = _renderable(a_name)[:16] or "Someone"
    b_disp = _renderable(b_name)[:16] or "Someone"
    for name, x in ((a_disp, left_x + avatar_size / 2), (b_disp, right_x + avatar_size / 2)):
        nb = draw.textbbox((0, 0), name, font=name_font)
        draw.text((x - (nb[2] - nb[0]) / 2, ay + avatar_size + 20), name, font=name_font, fill=(255, 255, 255))

    pct_text = f"{pct}% match"
    pb = draw.textbbox((0, 0), pct_text, font=pct_font)
    draw.text(((W - (pb[2] - pb[0])) / 2, ay + avatar_size + 90), pct_text, font=pct_font, fill=(80, 220, 120))

    out = io.BytesIO()
    out.name = "couple.png"
    img.convert("RGB").save(out, "PNG")
    out.seek(0)
    return out


# ───────── .couple ─────────
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

    pct = random.randint(40, 100)

    try:
        photo = await _build_couple_image(ctx, a_id, a_name, b_id, b_name, pct)
    except Exception as e:
        log.error("couple image build failed: %s", e)
        await say(ctx, chat.id, T("couldn't build that image:") + f" {esc(e)}", reply_to=msg.message_id)
        return

    a_mention = _html_mention(a_id, a_name)
    b_mention = _html_mention(b_id, b_name)
    cap = q(T("<b>💞 today's couple</b>\n{a} + {b}", a=a_mention, b=b_mention))

    await ctx.bot.send_photo(
        chat.id, photo, caption=cap, parse_mode=ParseMode.HTML,
        reply_parameters=ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True),
        has_spoiler=True,
    )


def register(app):
    dual_command(app, "waifu", waifu_cmd, group_only=False)
    dual_command(app, "couple", couple_cmd, group_only=False)
