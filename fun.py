"""Fun plugin: .waifu (find a waifu from the group) and .couple (ship two members)."""
import io
import logging
import random
import re
import time

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, ReplyParameters
from telegram.constants import ChatType, ParseMode
from telegram.ext import CallbackQueryHandler

import database as dbase
import sticker
from common import T, dual_command, esc, mention, q, say

log = logging.getLogger("fun")

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
async def _download_photo_bytes(ctx, user_id: int):
    """Download the user's real profile photo (raw bytes). None if unavailable.

    Tries: 1) getUserProfilePhotos  2) getChat(user).photo (fallback).
    """
    file_ids = []
    try:
        photos = await ctx.bot.get_user_profile_photos(user_id, limit=1)
        log.info("profile photos for %s: total=%s", user_id, photos.total_count)
        if photos.total_count > 0 and photos.photos:
            file_ids.append(photos.photos[0][-1].file_id)
    except Exception as e:
        log.warning("get_user_profile_photos failed for %s: %s", user_id, e)

    try:
        chat_obj = await ctx.bot.get_chat(user_id)
        if getattr(chat_obj, "photo", None):
            file_ids.append(chat_obj.photo.big_file_id)
    except Exception as e:
        log.warning("get_chat photo failed for %s: %s", user_id, e)

    for fid in file_ids:
        try:
            tg_file = await ctx.bot.get_file(fid)
            bio = io.BytesIO()
            await tg_file.download_to_memory(bio)
            data = bio.getvalue()
            if data:
                return data
        except Exception as e:
            log.warning("photo download failed for %s: %s", user_id, e)
    return None


async def _fetch_profile_photo(ctx, user_id: int):
    """Real Telegram profile photo as PIL Image, or None."""
    data = await _download_photo_bytes(ctx, user_id)
    if not data:
        return None
    try:
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception as e:
        log.warning("photo decode failed for %s: %s", user_id, e)
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

    # Real profile photo of the chosen user -> send as-is (spoiler)
    data = await _download_photo_bytes(ctx, target_id)
    if data:
        try:
            bio = io.BytesIO(data)
            bio.name = "waifu.jpg"
            await ctx.bot.send_photo(chat.id, bio, caption=cap,
                                     parse_mode=ParseMode.HTML, reply_parameters=reply_params,
                                     has_spoiler=True)
            return
        except Exception as e:
            log.warning("sending real photo failed: %s", e)

    # No photo (or hidden by privacy) -> letter placeholder
    img = _placeholder_avatar(target_name, 512)
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


# ───────── fun actions (hug / kiss / slap / kill ...) ─────────
# OtakuGIFs API (waifu.pics is blocked on this server)
OTAKUGIFS_API = "https://api.otakugifs.xyz/gif?reaction="
WAIFU_API = "https://api.waifu.pics/sfw/" # Fallback

# Optional: paste your own media here (list of URLs or Telegram file_ids) to use it
# instead of the API for that command. Empty = API is used.
# Tenor links for kill
LOCAL_MEDIA = {
    "hug": [],
    "kiss": [],
    "slap": [],
    "kill": [
        "https://c.tenor.com/2b1n1aX7x3gAAAAC/anime-kill.gif",
        "https://c.tenor.com/1Y9x2b1n1aX7x3gAAAAC/akame-ga-kill.gif",
        "https://c.tenor.com/8R2b1n1aX7x3gAAAAC/anime-sword.gif"
    ],
    "pat": [],
}

# Commands that need a target user (reply / @username / text-mention).
# "ask" = target must press Accept first.  "kind": nice | mean (used for bot-target replies)
TARGETED = {
    "hug": {
        "cat": "hug", "kind": "nice",
        "ask": "🤗 {b}, {a} wants to give you a hug!\n\nWill you accept?",
        "done": "💞 {b} accepted the hug from {a}! 💞",
        "desc": "Hug someone",
    },
    "kiss": {
        "cat": "kiss", "kind": "nice",
        "ask": "💋 {b}, {a} wants to kiss you!\n\nWill you accept?",
        "done": "💓 {b} accepted the kiss from {a}! 💓",
        "desc": "Kiss someone",
    },
    "cuddle": {
        "cat": "cuddle", "kind": "nice",
        "ask": "🥰 {b}, {a} wants to cuddle with you!\n\nWill you accept?",
        "done": "🥰 {b} is cuddling with {a}! 💞",
        "desc": "Cuddle someone",
    },
    "pat": {
        "cat": "pat", "kind": "nice",
        "done": "💖 {a} gave a warm pat to {b}! So sweet!",
        "desc": "Pat someone",
    },
    "handhold": {
        "cat": "handhold", "kind": "nice",
        "done": "🤝 {a} is holding hands with {b}! 💕",
        "desc": "Hold someone's hand",
    },
    "highfive": {
        "cat": "highfive", "kind": "nice",
        "done": "🙌 {a} high-fived {b}!",
        "desc": "High-five someone",
    },
    "poke": {
        "cat": "poke", "kind": "mean",
        "done": "👉 {a} poked {b}!",
        "desc": "Poke someone",
    },
    "slap": {
        "cat": "slap", "kind": "mean",
        "done": "👋 {a} slapped {b}! That must've hurt! 💥",
        "desc": "Slap someone",
    },
    "kill": {
        "cat": "kill", "kind": "mean",
        "done": "💀 {a} has killed {b}! 😱",
        "desc": "Kill someone (fun)",
    },
    "bite": {
        "cat": "bite", "kind": "mean",
        "done": "😬 {a} bit {b}! Chomp!",
        "desc": "Bite someone",
    },
    "bonk": {
        "cat": "bonk", "kind": "mean",
        "done": "🔨 {a} bonked {b} on the head!",
        "desc": "Bonk someone",
    },
}

# Solo reactions (no target needed)
SOLO = {
    "smile": ("😊 {a} is smiling!", "Smile"),
    "blush": ("😳 {a} is blushing!", "Blush"),
    "cry": ("😢 {a} is crying...", "Cry"),
    "happy": ("😄 {a} is so happy!", "Be happy"),
    "wink": ("😉 {a} winked!", "Wink"),
    "dance": ("💃 {a} is dancing!", "Dance"),
    "wave": ("👋 {a} waved hello!", "Wave hello"),
    "smug": ("😏 {a} looks smug.", "Smug face"),
    "cringe": ("😬 {a} is cringing...", "Cringe"),
}

# Plain random anime images (no caption)
IMAGE_ONLY = {
    "neko": "Random neko image",
    "shinobu": "Random Shinobu image",
    "megumin": "Random Megumin image",
}

ANIM_EXT = {"gif", "mp4", "webm"}


async def _get_media_url(action: str, category: str):
    """Own media (LOCAL_MEDIA) first, else OtakuGIFs API, then waifu.pics."""
    local = LOCAL_MEDIA.get(action)
    if local:
        return random.choice(local)
    
    # 1. Try OtakuGIFs
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(OTAKUGIFS_API + category)
            if r.status_code == 200:
                url = r.json().get("url")
                if url:
                    return url
    except Exception as e:
        log.warning("otakugifs fetch failed for %s: %s", category, e)

    # 2. Fallback to waifu.pics (for neko, shinobu, etc.)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(WAIFU_API + category)
            if r.status_code == 200:
                url = r.json().get("url")
                if url:
                    return url
    except Exception as e:
        log.warning("waifu.pics fetch failed for %s: %s", category, e)

    return None


async def _send_media(ctx, chat_id, url, caption=None, reply_params=None):
    """Send gif/mp4 as animation, everything else as photo. Falls back to text."""
    kw = {"chat_id": chat_id, "caption": caption, "parse_mode": ParseMode.HTML}
    if reply_params is not None:
        kw["reply_parameters"] = reply_params
    
    if url:
        # Check if it's a Telegram file_id (doesn't start with http)
        is_file_id = not url.startswith("http")
        ext = url.split("?")[0].lower().rsplit(".", 1)[-1]
        
        try:
            if is_file_id or ext in ANIM_EXT:
                await ctx.bot.send_animation(animation=url, **kw)
            else:
                await ctx.bot.send_photo(photo=url, **kw)
            return True
        except Exception as e:
            log.warning("sending media failed (%s): %s", url, e)
            # Fallback to photo if animation fails and it's a file_id
            if is_file_id:
                try:
                    await ctx.bot.send_photo(photo=url, **kw)
                    return True
                except Exception as e2:
                    log.warning("sending photo failed (%s): %s", url, e2)

    if caption:
        try:
            await ctx.bot.send_message(
                chat_id, caption, parse_mode=ParseMode.HTML,
                reply_parameters=reply_params,
            )
        except Exception as e:
            log.warning("fallback text failed: %s", e)
    return False


async def _resolve_target(msg, chat_id):
    """Return (user_id, name) from reply / text-mention / @username / numeric id, else None."""
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return u.id, (u.first_name or "Someone")

    for ent in (msg.entities or []):
        if ent.type == MessageEntity.TEXT_MENTION and ent.user:
            return ent.user.id, (ent.user.first_name or "Someone")

    parts = (msg.text or msg.caption or "").split()
    if len(parts) < 2:
        return None
    arg = parts[1].strip()

    if arg.isdigit():
        doc = await dbase.db.members.find_one({"chat_id": chat_id, "user_id": int(arg)}) or {}
        return int(arg), (doc.get("name") or "Someone")

    uname = arg.lstrip("@")
    if not uname:
        return None
    try:
        doc = await dbase.db.members.find_one({
            "chat_id": chat_id,
            "username": {"$regex": f"^{re.escape(uname)}$", "$options": "i"},
        })
    except Exception as e:
        log.warning("username lookup failed: %s", e)
        doc = None
    if doc and doc.get("user_id"):
        return doc["user_id"], (doc.get("name") or uname)
    return None


def _make_targeted(name, cfg):
    async def handler(update, ctx):
        msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
        if chat.type == ChatType.PRIVATE:
            await say(ctx, chat.id, T("this only works inside groups."))
            return

        target = await _resolve_target(msg, chat.id)
        if not target:
            await say(ctx, chat.id,
                      T("reply to someone's message or give a @username to use this command."),
                      reply_to=msg.message_id)
            return
        b_id, b_name = target

        if b_id == ctx.bot.id:
            if cfg["kind"] == "nice":
                txt = "aww, thanks! but I'm just a bot 🥺"
            else:
                txt = f"hey, don't {name} me! 😤"
            await say(ctx, chat.id, T(txt), reply_to=msg.message_id)
            return
        if b_id == user.id:
            await say(ctx, chat.id, T("you need someone else for that 😅"), reply_to=msg.message_id)
            return

        a_m = _html_mention(user.id, user.first_name or "Someone")
        b_m = _html_mention(b_id, b_name)
        reply_params = ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True)

        if cfg.get("ask"):
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("Accept ✅", callback_data=f"funacc:{name}:{user.id}:{b_id}")
            ]])
            await ctx.bot.send_message(
                chat.id, q(T(cfg["ask"], a=a_m, b=b_m)),
                parse_mode=ParseMode.HTML, reply_markup=kb, reply_parameters=reply_params,
            )
            return

        url = await _get_media_url(name, cfg["cat"])
        cap = q(T(cfg["done"], a=a_m, b=b_m))
        await _send_media(ctx, chat.id, url, cap, reply_params)

    handler.__name__ = f"{name}_cmd"
    return handler


async def _accept_cb(update, ctx):
    query = update.callback_query
    try:
        _, name, a_id, b_id = query.data.split(":")
        a_id, b_id = int(a_id), int(b_id)
    except Exception:
        await query.answer()
        return
    cfg = TARGETED.get(name)
    if not cfg:
        await query.answer()
        return

    if query.from_user.id != b_id:
        await query.answer("Only the recipient can accept this request.", show_alert=True)
        return

    chat_id = query.message.chat.id
    try:
        a_user = (await ctx.bot.get_chat_member(chat_id, a_id)).user
        a_name = a_user.first_name or "Someone"
    except Exception:
        a_name = "Someone"
    a_m = _html_mention(a_id, a_name)
    b_m = _html_mention(b_id, query.from_user.first_name or "Someone")

    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass

    url = await _get_media_url(name, cfg["cat"])
    cap = q(T(cfg["done"], a=a_m, b=b_m))
    await _send_media(ctx, chat_id, url, cap)


def _make_solo(name, text):
    async def handler(update, ctx):
        msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
        a_m = _html_mention(user.id, user.first_name or "Someone")
        url = await _get_media_url(name, name)
        cap = q(T(text, a=a_m))
        reply_params = ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True)
        await _send_media(ctx, chat.id, url, cap, reply_params)

    handler.__name__ = f"{name}_cmd"
    return handler


def _make_image_only(name):
    async def handler(update, ctx):
        msg, chat = update.effective_message, update.effective_chat
        url = await _get_media_url(name, name)
        if not url:
            await say(ctx, chat.id, T("couldn't fetch an image right now, try again later."),
                      reply_to=msg.message_id)
            return
        reply_params = ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True)
        await _send_media(ctx, chat.id, url, None, reply_params)

    handler.__name__ = f"{name}_cmd"
    return handler


# ───────── help + command menu (built from the tables above) ─────────
COMMANDS = [("waifu", "Find a waifu from this group"), ("couple", "Ship two members")]
COMMANDS += [(n, c["desc"]) for n, c in TARGETED.items()]
COMMANDS += [(n, d) for n, (_, d) in SOLO.items()]
COMMANDS += list(IMAGE_ONLY.items())

HELP_TXT = (
    "<b>✦ fun</b>\n\n"
    "/waifu (or .waifu) — find your waifu from this group (changes every 24h)\n"
    "/couple (or .couple) — ship two members of this group (changes every 24h)\n\n"
    "<b>With a target</b> (reply to someone or give @username):\n"
    + ", ".join("/" + n for n in TARGETED) + "\n"
    "<i>hug, kiss and cuddle need the other person to press Accept.</i>\n\n"
    "<b>Reactions</b>:\n"
    + ", ".join("/" + n for n in SOLO) + "\n\n"
    "<b>Anime images</b>:\n"
    + ", ".join("/" + n for n in IMAGE_ONLY)
)


def register(app):
    dual_command(app, "waifu", waifu_cmd, group_only=False)
    dual_command(app, "couple", couple_cmd, group_only=False)

    for name, cfg in TARGETED.items():
        dual_command(app, name, _make_targeted(name, cfg), group_only=False)
    for name, (text, _) in SOLO.items():
        dual_command(app, name, _make_solo(name, text), group_only=False)
    for name in IMAGE_ONLY:
        dual_command(app, name, _make_image_only(name), group_only=False)

    app.add_handler(CallbackQueryHandler(_accept_cb, pattern=r"^funacc:"))
    
