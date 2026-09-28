import html
import logging
import os
import re
from collections import OrderedDict

import database as dbase
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    ReplyParameters,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("bot")

# ───────────────────────── CONFIG ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
DB_NAME = os.getenv("DB_NAME", "zoya_bot")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "Ownerbackk").lstrip("@")
START_IMG = os.getenv(
    "START_IMG",
    "https://graph.org/file/d3a2c17942e606f4ec811-9c0373fa8bb10f4448.jpg",
)

OWNER, ADMIN = ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR

# ───────────────────── STYLE HELPERS ─────────────────────
_SC = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))
assert len(_SC) == 26
# tags, {placeholders}, /commands and @names are NOT converted
_SKIP = re.compile(r"(<[^>]+>|\{[^}]*\}|(?<![\w/])/[a-z_]+|@\w+)")


def sc(text: str) -> str:
    """Small-caps font for static text."""
    parts = _SKIP.split(text)
    return "".join(
        p if i % 2 else "".join(_SC.get(c, c) for c in p.lower())
        for i, p in enumerate(parts)
    )


def T(text: str, **kw) -> str:
    """Small-caps static text, then fill {placeholders} with (already escaped) values."""
    return sc(text).format(**kw)


def esc(t) -> str:
    return html.escape(str(t), quote=False)


def q(text: str) -> str:
    """Everything the bot says goes inside a quote block."""
    return f"<blockquote>{text}</blockquote>"


def mention(u) -> str:
    name = getattr(u, "full_name", None) or str(u.id)
    return f'<a href="tg://user?id={u.id}">{esc(name)}</a>'


def B(text, data=None, url=None, style=None) -> InlineKeyboardButton:
    """Inline button. style: 'primary' (blue) | 'success' (green) | 'danger' (red)."""
    kw = {"api_kwargs": {"style": style}} if style else {}
    return InlineKeyboardButton(text, callback_data=data, url=url, **kw)


async def say(ctx, chat_id, text, kb=None, reply_to=None):
    rp = (
        ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        if reply_to
        else None
    )
    return await ctx.bot.send_message(
        chat_id, q(text), parse_mode=ParseMode.HTML, reply_markup=kb, reply_parameters=rp
    )


cfg_get = dbase.cfg_get
cfg_set = dbase.cfg_set


# ───────────────────── PERMISSION HELPERS ─────────────────────
RIGHTS = [
    ("change_info", "Change Info"),
    ("delete_messages", "Delete Messages"),
    ("restrict_members", "Ban Users"),
    ("invite_users", "Invite Users"),
    ("pin_messages", "Pin Messages"),
    ("manage_video_chats", "Video Chats"),
    ("manage_topics", "Topics"),
    ("promote_members", "Add Admins"),
]
RL = dict(RIGHTS)


async def get_member(ctx, chat_id, user_id):
    try:
        return await ctx.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return None


def rights_of(m) -> dict:
    if m is None:
        return {k: False for k, _ in RIGHTS}
    if m.status == OWNER:
        return {k: True for k, _ in RIGHTS}
    if m.status == ADMIN:
        return {k: bool(getattr(m, f"can_{k}", False)) for k, _ in RIGHTS}
    return {k: False for k, _ in RIGHTS}


async def perm_report(ctx, chat) -> str:
    m = await get_member(ctx, chat.id, ctx.bot.id)
    if not m or m.status not in (ADMIN, OWNER):
        return T("<b>bot status:</b> not admin ❌\nmake me admin so every feature works.")
    r = rights_of(m)
    lines = [T("<b>bot status:</b> admin ✅")]
    for k, label in RIGHTS:
        if k == "manage_topics" and not chat.is_forum:
            continue
        lines.append(f"{'✅' if r[k] else '❌'} {label}")
    return "\n".join(lines)


async def require_admin(update: Update, ctx, right=None) -> bool:
    """Checks that the command sender is an admin (optionally with a specific right)."""
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        await say(ctx, chat.id, T("this command works only in groups."))
        return False
    if msg.sender_chat and msg.sender_chat.id == chat.id:  # anonymous admin
        return True
    user = update.effective_user
    m = await get_member(ctx, chat.id, user.id)
    if not m or m.status not in (OWNER, ADMIN):
        await say(ctx, chat.id, T("only group admins can use this command."), reply_to=msg.message_id)
        return False
    if right and m.status == ADMIN and not getattr(m, f"can_{right}", False):
        await say(
            ctx,
            chat.id,
            T("{m}, you don't have the {l} right.", m=mention(user), l=f"<b>{RL[right]}</b>"),
            reply_to=msg.message_id,
        )
        return False
    return True


async def tag_missing(ctx, chat_id, user, labels, reply_to=None):
    await say(
        ctx,
        chat_id,
        T(
            "{m}, i don't have the {l} power. please give it to me and try again.",
            m=mention(user),
            l="<b>" + esc(", ".join(labels)) + "</b>",
        ),
        reply_to=reply_to,
    )


def explain(e: Exception) -> str:
    s = str(e).lower()
    if "not enough rights" in s or "chat_admin_required" in s or "right_forbidden" in s:
        return "I don't have enough rights for this (Add Admins or one of the selected powers)."
    if "user_creator" in s or "chat owner" in s:
        return "This user is the group owner. Nothing can be changed."
    if "user_not_participant" in s or "participant_id_invalid" in s:
        return "This user is not in the group."
    return f"Telegram said: {esc(e)}"


# ───────────────────── /START + HELP ─────────────────────
START_TXT = (
    "<b>✦ hey {m} !</b>\n\n"
    "i am <b>𝙕𝙤𝙮𝙖</b> — a powerful group management bot.\n"
    "➤ stylish welcome messages\n"
    "➤ promote / demote with power panel\n"
    "➤ smart permission checks everywhere\n\n"
    "tap <b>commands</b> to see everything i can do."
)

GREET_TXT = (
    "<b>✦ greetings — welcome system</b>\n\n"
    "/setwelcome — reply to any message (text, photo, video, gif, sticker) or type text after it\n"
    "/welcome — status + preview • /welcome on|off\n"
    "/resetwelcome — back to default\n"
    "/cleanwelcome on|off — delete old welcome when a new member joins\n"
    "/cleanservice on|off — delete 'user joined' service messages\n"
    "/setrules • /rules — group rules\n\n"
    "<b>fillings:</b> {first} {last} {fullname} {username} {mention} {id} {chatname} {count}\n\n"
    "bold, italic, quote, links, spoiler and custom emoji stay exactly as you write them.\n"
    "only admins with change info can set these. i verify my own permissions and tell you what is missing."
)

ADMIN_TXT = (
    "<b>✦ admin — promote & demote</b>\n\n"
    "/promote — reply / @username / id (+ optional custom title). "
    "a panel opens: tap ✅ / ❌ to choose powers, ⚡ full power, then ✅ promote.\n"
    "/demote — removes all admin powers after confirmation\n"
    "/botperms — shows which powers i have in the group\n\n"
    "every action is verified: you need add admins, and i must have it too — plus every power i give. "
    "if i lack something i tag you and say so. 🔒 buttons = powers i don't have."
)


def add_me_url(username: str) -> str:
    rights = (
        "change_info+delete_messages+restrict_members+invite_users+pin_messages"
        "+manage_video_chats+manage_topics+promote_members+manage_chat"
    )
    return f"https://t.me/{username}?startgroup=true&admin={rights}"


def help_page(page: str, user, ctx):
    me = ctx.application.bot_data["me"]
    if page == "main":
        return (
            sc("<b>✦ commands</b>\n\nchoose a category below to see all details."),
            InlineKeyboardMarkup(
                [
                    [
                        B("🎉 Greetings", "help:greet", style="success"),
                        B("👮 Admin", "help:admin", style="primary"),
                    ],
                    [B("⬅ Back", "help:home"), B("✖ Close", "help:close", style="danger")],
                ]
            ),
        )
    if page in ("greet", "admin"):
        return (
            sc(GREET_TXT if page == "greet" else ADMIN_TXT),
            InlineKeyboardMarkup(
                [[B("⬅ Back", "help:main"), B("✖ Close", "help:close", style="danger")]]
            ),
        )
    return (
        T(START_TXT, m=mention(user)),
        InlineKeyboardMarkup(
            [
                [B("📜 Commands", "help:main", style="primary")],
                [
                    B("➕ Add Me", url=add_me_url(me.username), style="success"),
                    B("👑 Owner", url=f"https://t.me/{OWNER_USERNAME}", style="primary"),
                ],
            ]
        ),
    )


async def edit_page(qy, text, kb):
    m = qy.message
    try:
        if m.photo or m.video or m.animation:
            await qy.edit_message_caption(caption=q(text), reply_markup=kb)
        else:
            await qy.edit_message_text(q(text), reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    me = ctx.application.bot_data["me"]
    if chat.type != ChatType.PRIVATE:
        kb = InlineKeyboardMarkup(
            [[B("💬 Open in DM", url=f"https://t.me/{me.username}?start=help", style="primary")]]
        )
        await say(
            ctx, chat.id, T("i am alive ✨ open my dm to see all commands."), kb=kb, reply_to=msg.message_id
        )
        return
    await dbase.save_user(user.id, user.first_name, user.username)
    text, kb = help_page("home", user, ctx)
    try:
        await ctx.bot.send_photo(chat.id, START_IMG, caption=q(text), reply_markup=kb)
    except TelegramError as e:
        log.warning("start image failed: %s", e)
        await ctx.bot.send_message(chat.id, q(text), reply_markup=kb)


async def help_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    qy = update.callback_query
    page = qy.data.split(":")[1]
    await qy.answer()
    if page == "close":
        try:
            await qy.message.delete()
        except TelegramError:
            pass
        return
    text, kb = help_page(page, qy.from_user, ctx)
    await edit_page(qy, text, kb)


# ───────────────────── WELCOME SYSTEM ─────────────────────
DEFAULT_WELCOME = {
    "type": "text",
    "file_id": None,
    "text": (
        "<blockquote>✦ <b>Welcome to {chatname}</b>\n\n"
        "➤ Dear - {first}\n➤ ID:- <code>{id}</code>\n➤ Username:- {username}</blockquote>\n"
        "<blockquote>Hey welcome, we're so happy to have you here! ✨\n"
        "Make yourself comfy, join the chat, meet new people &amp; enjoy the vibes.</blockquote>"
    ),
}


def media_of(m):
    if m.photo:
        return "photo", m.photo[-1].file_id
    if m.video:
        return "video", m.video.file_id
    if m.animation:
        return "animation", m.animation.file_id
    if m.sticker:
        return "sticker", m.sticker.file_id
    if m.document:
        return "document", m.document.file_id
    return "text", None


def extract(msg, cmd: str):
    src = msg.reply_to_message
    if src:
        kind, fid = media_of(src)
        text = src.text_html if src.text else (src.caption_html or "")
    else:
        kind, fid = media_of(msg)
        raw = msg.text_html if msg.text else (msg.caption_html or "")
        text = re.sub(rf"^/{cmd}(@\w+)?\s*", "", raw or "", flags=re.I)
    text = (text or "").strip()
    if kind == "text" and not text:
        return None
    return {"type": kind, "file_id": fid, "text": text}


def render(tpl: str, user, chat, count) -> str:
    rep = {
        "{first}": esc(user.first_name or ""),
        "{last}": esc(user.last_name or ""),
        "{fullname}": esc(user.full_name or ""),
        "{username}": f"@{esc(user.username)}" if user.username else mention(user),
        "{mention}": mention(user),
        "{id}": str(user.id),
        "{chatname}": esc(chat.title or ""),
        "{count}": str(count),
    }
    for k, v in rep.items():
        tpl = tpl.replace(k, v)
    return tpl


async def send_welcome(ctx, chat, user, cfg):
    w = cfg.get("welcome") or DEFAULT_WELCOME
    try:
        count = await ctx.bot.get_chat_member_count(chat.id)
    except TelegramError:
        count = "?"
    text = render(w.get("text") or "", user, chat, count)
    kind, fid = w.get("type", "text"), w.get("file_id")

    async def _send(parse_mode):
        cap = text or None
        if kind == "photo":
            return await ctx.bot.send_photo(chat.id, fid, caption=cap, parse_mode=parse_mode)
        if kind == "video":
            return await ctx.bot.send_video(chat.id, fid, caption=cap, parse_mode=parse_mode)
        if kind == "animation":
            return await ctx.bot.send_animation(chat.id, fid, caption=cap, parse_mode=parse_mode)
        if kind == "document":
            return await ctx.bot.send_document(chat.id, fid, caption=cap, parse_mode=parse_mode)
        if kind == "sticker":
            s = await ctx.bot.send_sticker(chat.id, fid)
            if text:
                await ctx.bot.send_message(chat.id, text, parse_mode=parse_mode)
            return s
        return await ctx.bot.send_message(chat.id, text or "Welcome!", parse_mode=parse_mode)

    try:
        return await _send(ParseMode.HTML)
    except BadRequest as e:
        if "parse" in str(e).lower() or "entities" in str(e).lower():
            return await _send(None)
        raise


async def setwelcome_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.message, update.effective_chat
    if not msg:
        return
    if not await require_admin(update, ctx, "change_info"):
        return
    data = extract(msg, "setwelcome")
    if not data:
        await say(
            ctx,
            chat.id,
            T(
                "reply to a message with /setwelcome, or write text after it.\n"
                "photo / video / gif / sticker also work (send media with caption /setwelcome).\n"
                "fillings: {f}",
                f="{first} {last} {fullname} {username} {mention} {id} {chatname} {count}",
            ),
            reply_to=msg.message_id,
        )
        return
    await dbase.save_welcome(chat.id, data)
    cfg = await cfg_get(chat.id)
    try:
        await send_welcome(ctx, chat, msg.from_user, cfg)
        prev = T("✅ welcome saved (type: {t}). preview sent above.", t=data["type"])
    except TelegramError as e:
        prev = T("⚠ saved, but preview failed:") + f" {esc(e)}"
    bm = await get_member(ctx, chat.id, ctx.bot.id)
    extra = ""
    if not rights_of(bm)["delete_messages"]:
        extra = "\n\n" + T(
            "{m}, i don't have the {l} power. /cleanwelcome and /cleanservice will not work until you give it.",
            m=mention(msg.from_user),
            l="<b>Delete Messages</b>",
        )
    await say(ctx, chat.id, prev + "\n\n" + await perm_report(ctx, chat) + extra, reply_to=msg.message_id)


async def welcome_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.message, update.effective_chat
    if not msg:
        return
    if not await require_admin(update, ctx, "change_info"):
        return
    cfg = await cfg_get(chat.id)
    arg = ctx.args[0].lower() if ctx.args else ""
    if arg in ("on", "off", "yes", "no"):
        val = arg in ("on", "yes")
        await cfg_set(chat.id, welcome_on=val)
        await say(
            ctx,
            chat.id,
            T("welcome messages are now {s}.", s="<b>ON ✅</b>" if val else "<b>OFF ❌</b>"),
            reply_to=msg.message_id,
        )
        return
    w = cfg.get("welcome") or DEFAULT_WELCOME
    status = T(
        "<b>welcome settings</b>\nwelcome: {a}\nclean welcome: {b}\nclean service: {c}\nmessage type: {t}",
        a="ON ✅" if cfg.get("welcome_on", True) else "OFF ❌",
        b="ON ✅" if cfg.get("clean_welcome") else "OFF ❌",
        c="ON ✅" if cfg.get("clean_service") else "OFF ❌",
        t=w.get("type", "text"),
    )
    await say(ctx, chat.id, status + "\n\n" + await perm_report(ctx, chat), reply_to=msg.message_id)
    try:
        await send_welcome(ctx, chat, msg.from_user, cfg)
    except TelegramError as e:
        await say(ctx, chat.id, T("preview failed:") + f" {esc(e)}")


async def resetwelcome_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.message, update.effective_chat
    if not msg or not await require_admin(update, ctx, "change_info"):
        return
    await dbase.reset_welcome(chat.id)
    await say(ctx, chat.id, T("welcome reset to default ✅"), reply_to=msg.message_id)


async def _toggle(update, ctx, key, label, cmd):
    msg, chat = update.message, update.effective_chat
    if not msg or not await require_admin(update, ctx, "change_info"):
        return
    arg = ctx.args[0].lower() if ctx.args else ""
    cfg = await cfg_get(chat.id)
    if arg not in ("on", "off", "yes", "no"):
        cur = "ON ✅" if cfg.get(key) else "OFF ❌"
        await say(
            ctx,
            chat.id,
            T("{l} is currently {c}.\nusage: /{cmd} on|off", l=label, c=cur, cmd=cmd),
            reply_to=msg.message_id,
        )
        return
    val = arg in ("on", "yes")
    if val:  # both features need the delete-messages power
        bm = await get_member(ctx, chat.id, ctx.bot.id)
        if not rights_of(bm)["delete_messages"]:
            await tag_missing(ctx, chat.id, msg.from_user, ["Delete Messages"], msg.message_id)
            return
    await cfg_set(chat.id, **{key: val})
    await say(
        ctx,
        chat.id,
        T("{l} is now {s}.", l=label, s="<b>ON ✅</b>" if val else "<b>OFF ❌</b>"),
        reply_to=msg.message_id,
    )


async def cleanwelcome_cmd(update, ctx):
    await _toggle(update, ctx, "clean_welcome", "clean welcome", "cleanwelcome")


async def cleanservice_cmd(update, ctx):
    await _toggle(update, ctx, "clean_service", "clean service", "cleanservice")


async def setrules_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.message, update.effective_chat
    if not msg or not await require_admin(update, ctx, "change_info"):
        return
    data = extract(msg, "setrules")
    if not data or not data["text"]:
        await say(
            ctx,
            chat.id,
            T("reply to a text message with /setrules or write the rules after it."),
            reply_to=msg.message_id,
        )
        return
    await cfg_set(chat.id, rules=data["text"])
    await say(ctx, chat.id, T("rules saved ✅ members can use /rules"), reply_to=msg.message_id)


async def rules_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.message, update.effective_chat
    if not msg:
        return
    if chat.type == ChatType.PRIVATE:
        await say(ctx, chat.id, T("use this command in a group."))
        return
    cfg = await cfg_get(chat.id)
    if not cfg.get("rules"):
        await say(ctx, chat.id, T("no rules set yet. admins can use /setrules."), reply_to=msg.message_id)
        return
    await ctx.bot.send_message(
        chat.id,
        q(cfg["rules"]),
        parse_mode=ParseMode.HTML,
        reply_parameters=ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True),
    )


async def botperms_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.message, update.effective_chat
    if not msg or not await require_admin(update, ctx):
        return
    await say(ctx, chat.id, await perm_report(ctx, chat), reply_to=msg.message_id)


async def on_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.message, update.effective_chat
    if not msg or not msg.new_chat_members:
        return
    cfg = await cfg_get(chat.id)
    if cfg.get("clean_service"):
        try:
            await msg.delete()
        except TelegramError:
            pass
    humans = []
    for u in msg.new_chat_members:
        if u.id == ctx.bot.id:
            await say(
                ctx,
                chat.id,
                T("thanks for adding me! ✨ make me admin so every feature works.")
                + "\n\n"
                + await perm_report(ctx, chat),
            )
        elif not u.is_bot:
            humans.append(u)
    if not humans or not cfg.get("welcome_on", True):
        return
    for u in humans[:5]:
        if cfg.get("clean_welcome") and cfg.get("last_welcome"):
            try:
                await ctx.bot.delete_message(chat.id, cfg["last_welcome"])
            except TelegramError:
                pass
        try:
            sent = await send_welcome(ctx, chat, u, cfg)
            cfg["last_welcome"] = sent.message_id
            await cfg_set(chat.id, last_welcome=sent.message_id)
        except TelegramError as e:
            log.warning("welcome failed in %s: %s", chat.id, e)


# ───────────────────── PROMOTE / DEMOTE ─────────────────────
PANELS: "OrderedDict[str, dict]" = OrderedDict()


def panel_put(key, st):
    PANELS[key] = st
    while len(PANELS) > 300:
        PANELS.popitem(last=False)


_seen: dict = {}


async def track(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Caches username -> id so /promote @username works."""
    u = update.effective_user
    if u and u.username and not u.is_bot and _seen.get(u.id) != u.username:
        _seen[u.id] = u.username
        await dbase.save_user(u.id, u.first_name, u.username)


async def resolve_target(update, ctx):
    msg, chat = update.effective_message, update.effective_chat
    args = list(ctx.args or [])
    rep = msg.reply_to_message
    if rep and rep.from_user and not rep.sender_chat:
        return rep.from_user, " ".join(args).strip()
    if not args:
        return None, ""
    title = " ".join(args[1:]).strip()
    for ent in msg.entities or []:
        if ent.type == "text_mention" and ent.user:
            return ent.user, title
    a, uid = args[0], None
    if a.lstrip("-").isdigit():
        uid = int(a)
    elif a.startswith("@"):
        uid = await dbase.get_user_id_by_username(a)
        if uid is None:
            try:
                c = await ctx.bot.get_chat(a)
                if c.type == ChatType.PRIVATE:
                    uid = c.id
            except TelegramError:
                pass
    if uid is None:
        return None, ""
    m = await get_member(ctx, chat.id, uid)
    return (m.user if m else None), title


async def precheck(update, ctx, mode: str):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        me = ctx.application.bot_data["me"]
        kb = InlineKeyboardMarkup(
            [[B("➕ Add Me To Group", url=add_me_url(me.username), style="success")]]
        )
        await say(
            ctx,
            chat.id,
            T("/promote and /demote work inside groups. add me to your group and make me admin."),
            kb=kb,
        )
        return None
    if msg.sender_chat:
        await say(
            ctx,
            chat.id,
            T("you are anonymous. turn off 'remain anonymous' in your admin rights and try again."),
            reply_to=msg.message_id,
        )
        return None
    if not await require_admin(update, ctx, "promote_members"):
        return None
    target, title = await resolve_target(update, ctx)
    if not target:
        await say(
            ctx,
            chat.id,
            T("reply to a user, or use /{c} @username | user id", c=mode),
            reply_to=msg.message_id,
        )
        return None
    if target.id == ctx.bot.id:
        await say(ctx, chat.id, T("i can't do that to myself 🙂"), reply_to=msg.message_id)
        return None
    bm = await get_member(ctx, chat.id, ctx.bot.id)
    br = rights_of(bm)
    if not br["promote_members"]:
        if not bm or bm.status not in (ADMIN, OWNER):
            await say(
                ctx,
                chat.id,
                T(
                    "{m}, i am not an admin here. make me admin with the {l} power first.",
                    m=mention(user),
                    l="<b>Add New Admins</b>",
                ),
                reply_to=msg.message_id,
            )
        else:
            await tag_missing(ctx, chat.id, user, ["Add New Admins"], msg.message_id)
        return None
    tm = await get_member(ctx, chat.id, target.id)
    if tm is None or tm.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        await say(ctx, chat.id, T("that user is not in this group."), reply_to=msg.message_id)
        return None
    if tm.status == OWNER:
        await say(
            ctx, chat.id, T("that user is the group owner. nothing can be changed."), reply_to=msg.message_id
        )
        return None
    if tm.status == ADMIN and not tm.can_be_edited:
        await say(
            ctx,
            chat.id,
            T(
                "{m} is an admin promoted by someone else, so i can't edit their powers. only that admin or the owner can.",
                m=mention(target),
            ),
            reply_to=msg.message_id,
        )
        return None
    if mode == "demote" and tm.status != ADMIN:
        await say(ctx, chat.id, T("{m} is not an admin.", m=mention(target)), reply_to=msg.message_id)
        return None
    return dict(chat=chat, user=user, target=target, title=title[:16], tm=tm, br=br, msg=msg)


def panel_text(st) -> str:
    lines = [
        T("<b>✦ promote panel</b>"),
        "",
        T("user: ") + st["tgt_m"],
        T("select the powers, then press promote."),
    ]
    if st["title"]:
        lines.append(T("title: ") + f"<b>{esc(st['title'])}</b>")
    if st["missing"]:
        lines += [
            "",
            T(
                "{m}, i don't have: {l}. those buttons are locked 🔒",
                m=st["inv_m"],
                l="<b>" + esc(", ".join(st["missing"])) + "</b>",
            ),
        ]
    return "\n".join(lines)


def panel_kb(st):
    btns = []
    for k, label in st["rights_list"]:
        if not st["bot"].get(k):
            btns.append(B(f"🔒 {label}", f"pr:na:{k}"))
        elif st["sel"].get(k):
            btns.append(B(f"✅ {label}", f"pr:t:{k}", style="success"))
        else:
            btns.append(B(f"❌ {label}", f"pr:t:{k}", style="danger"))
    anon = st["sel"].get("anonymous")
    btns.append(
        B(f"{'✅' if anon else '❌'} Anonymous", "pr:t:anonymous", style="success" if anon else "danger")
    )
    rows = [btns[i : i + 2] for i in range(0, len(btns), 2)]
    rows.append([B("⚡ Full Power", "pr:full", style="primary"), B("🧹 Clear All", "pr:clear")])
    rows.append([B("✅ Promote", "pr:go", style="success"), B("✖ Cancel", "pr:x", style="danger")])
    return InlineKeyboardMarkup(rows)


async def promote_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    p = await precheck(update, ctx, "promote")
    if not p:
        return
    chat, user, target, tm, br = p["chat"], p["user"], p["target"], p["tm"], p["br"]
    rights_list = [(k, l) for k, l in RIGHTS if k != "manage_topics" or chat.is_forum]
    if tm.status == ADMIN:
        cur = rights_of(tm)
        sel = {k: cur[k] and br[k] for k, _ in rights_list}
        sel["anonymous"] = bool(tm.is_anonymous)
    else:
        sel = {
            k: (k in ("delete_messages", "invite_users", "pin_messages")) and br[k]
            for k, _ in rights_list
        }
    st = dict(
        mode="promote",
        chat_id=chat.id,
        invoker=user.id,
        target=target.id,
        tgt_m=mention(target),
        inv_m=mention(user),
        title=p["title"],
        rights_list=rights_list,
        bot=br,
        sel=sel,
        forum=bool(chat.is_forum),
        missing=[l for k, l in rights_list if not br[k]],
    )
    sent = await ctx.bot.send_message(
        chat.id,
        q(panel_text(st)),
        reply_markup=panel_kb(st),
        reply_parameters=ReplyParameters(message_id=p["msg"].message_id, allow_sending_without_reply=True),
    )
    panel_put(f"{chat.id}:{sent.message_id}", st)


async def demote_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    p = await precheck(update, ctx, "demote")
    if not p:
        return
    chat, user, target = p["chat"], p["user"], p["target"]
    st = dict(
        mode="demote",
        chat_id=chat.id,
        invoker=user.id,
        target=target.id,
        tgt_m=mention(target),
        inv_m=mention(user),
        forum=bool(chat.is_forum),
    )
    kb = InlineKeyboardMarkup(
        [[B("✅ Yes, Demote", "dm:go", style="danger"), B("✖ Cancel", "dm:x", style="success")]]
    )
    sent = await ctx.bot.send_message(
        chat.id,
        q(T("<b>⚠ demote</b>\n\nremove all admin powers of {m}?", m=st["tgt_m"])),
        reply_markup=kb,
        reply_parameters=ReplyParameters(message_id=p["msg"].message_id, allow_sending_without_reply=True),
    )
    panel_put(f"{chat.id}:{sent.message_id}", st)


async def _load_panel(update, ctx, mode):
    qy = update.callback_query
    m = qy.message
    key = f"{m.chat_id}:{m.message_id}"
    st = PANELS.get(key)
    if not st or st["mode"] != mode:
        await qy.answer("Panel expired. Run the command again.", show_alert=True)
        return None, None, None
    if qy.from_user.id != st["invoker"]:
        await qy.answer("This panel is not for you.", show_alert=True)
        return None, None, None
    return qy, st, key


async def _verify_now(ctx, qy, st):
    """Re-checks invoker + bot rights at click time. Returns bot rights or None."""
    cid = st["chat_id"]
    inv = await get_member(ctx, cid, qy.from_user.id)
    if (
        not inv
        or inv.status not in (OWNER, ADMIN)
        or (inv.status == ADMIN and not inv.can_promote_members)
    ):
        await qy.answer("You no longer have the Add Admins right.", show_alert=True)
        return None
    br = rights_of(await get_member(ctx, cid, ctx.bot.id))
    if not br["promote_members"]:
        await qy.answer("I don't have the Add New Admins power.", show_alert=True)
        await tag_missing(ctx, cid, qy.from_user, ["Add New Admins"])
        return None
    return br


async def promote_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    qy, st, key = await _load_panel(update, ctx, "promote")
    if not st:
        return
    parts = qy.data.split(":")
    action, arg = parts[1], (parts[2] if len(parts) > 2 else None)

    if action == "na":
        await qy.answer("❌ I don't have this power. Give it to me first.", show_alert=True)
        return
    if action == "x":
        PANELS.pop(key, None)
        await qy.edit_message_text(q(T("promotion cancelled ✖")))
        await qy.answer()
        return
    if action == "t":
        if arg != "anonymous" and not st["bot"].get(arg):
            await qy.answer("❌ I don't have this power.", show_alert=True)
            return
        st["sel"][arg] = not st["sel"].get(arg)
    elif action == "full":
        for k, _ in st["rights_list"]:
            st["sel"][k] = bool(st["bot"].get(k))
    elif action == "clear":
        st["sel"] = {}
    elif action == "go":
        br = await _verify_now(ctx, qy, st)
        if br is None:
            return
        chosen = [k for k, _ in st["rights_list"] if st["sel"].get(k)]
        lack = [RL[k] for k in chosen if not br.get(k)]
        if lack:
            await qy.answer("I lack some selected powers.", show_alert=True)
            await tag_missing(ctx, st["chat_id"], qy.from_user, lack)
            return
        if not chosen:
            await qy.answer("Select at least one power.", show_alert=True)
            return
        kw = {f"can_{k}": (k in chosen) for k, _ in st["rights_list"]}
        try:
            await ctx.bot.promote_chat_member(
                st["chat_id"],
                st["target"],
                can_manage_chat=True,
                is_anonymous=bool(st["sel"].get("anonymous")),
                **kw,
            )
        except TelegramError as e:
            await qy.answer("Failed, see message below.")
            await say(ctx, st["chat_id"], f"{st['inv_m']}, " + T("promotion failed:") + f" {explain(e)}")
            return
        note = ""
        if st["title"]:
            try:
                await ctx.bot.set_chat_administrator_custom_title(st["chat_id"], st["target"], st["title"])
                note = "\n" + T("title: ") + f"<b>{esc(st['title'])}</b>"
            except TelegramError as e:
                note = "\n" + T("title not set:") + f" {explain(e)}"
        powers = "\n".join(f"✅ {RL[k]}" for k in chosen)
        if st["sel"].get("anonymous"):
            powers += "\n✅ Anonymous"
        PANELS.pop(key, None)
        await qy.edit_message_text(
            q(T("<b>✅ promoted</b>\n\n{m}\n\n{p}{n}", m=st["tgt_m"], p=powers, n=note))
        )
        await qy.answer("Promoted ✅")
        return
    try:
        await qy.edit_message_text(q(panel_text(st)), reply_markup=panel_kb(st))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise
    await qy.answer()


async def demote_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    qy, st, key = await _load_panel(update, ctx, "demote")
    if not st:
        return
    action = qy.data.split(":")[1]
    if action == "x":
        PANELS.pop(key, None)
        await qy.edit_message_text(q(T("demotion cancelled ✖")))
        await qy.answer()
        return
    br = await _verify_now(ctx, qy, st)
    if br is None:
        return
    kw = {f"can_{k}": False for k, _ in RIGHTS if k != "manage_topics" or st["forum"]}
    try:
        await ctx.bot.promote_chat_member(
            st["chat_id"], st["target"], can_manage_chat=False, is_anonymous=False, **kw
        )
    except TelegramError as e:
        await qy.answer("Failed, see message below.")
        await say(ctx, st["chat_id"], f"{st['inv_m']}, " + T("demotion failed:") + f" {explain(e)}")
        return
    PANELS.pop(key, None)
    await qy.edit_message_text(q(T("<b>✅ demoted</b>\n\n{m} is no longer an admin.", m=st["tgt_m"])))
    await qy.answer("Demoted ✅")


# ───────────────────── STARTUP ─────────────────────
async def post_init(app: Application):
    await dbase.init(MONGO_URI, DB_NAME)
    app.bot_data["me"] = await app.bot.get_me()
    await app.bot.set_my_commands(
        [
            ("start", "Start the bot"),
            ("setwelcome", "Set welcome message"),
            ("welcome", "Welcome status / on / off"),
            ("cleanwelcome", "Delete old welcome messages"),
            ("cleanservice", "Delete join service messages"),
            ("resetwelcome", "Reset welcome"),
            ("setrules", "Set group rules"),
            ("rules", "Show group rules"),
            ("promote", "Promote a user"),
            ("demote", "Demote an admin"),
            ("botperms", "Check my permissions"),
        ]
    )
    log.info("Started as @%s", app.bot_data["me"].username)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=ctx.error)


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(
            Defaults(
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        )
        .post_init(post_init)
        .build()
    )
    grp = filters.ChatType.GROUPS
    app.add_handler(MessageHandler(filters.ALL & grp, track), group=-1)

    app.add_handler(CommandHandler(["start", "help"], start_cmd))
    app.add_handler(CommandHandler("setwelcome", setwelcome_cmd, filters=grp))
    app.add_handler(
        MessageHandler(
            filters.CaptionRegex(re.compile(r"^/setwelcome(@\w+)?(\s|$)", re.I)) & grp,
            setwelcome_cmd,
        )
    )
    app.add_handler(CommandHandler("welcome", welcome_cmd, filters=grp))
    app.add_handler(CommandHandler("resetwelcome", resetwelcome_cmd, filters=grp))
    app.add_handler(CommandHandler("cleanwelcome", cleanwelcome_cmd, filters=grp))
    app.add_handler(CommandHandler("cleanservice", cleanservice_cmd, filters=grp))
    app.add_handler(CommandHandler("setrules", setrules_cmd, filters=grp))
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("botperms", botperms_cmd, filters=grp))
    app.add_handler(CommandHandler("promote", promote_cmd))
    app.add_handler(CommandHandler("demote", demote_cmd))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_join))

    app.add_handler(CallbackQueryHandler(help_cb, pattern=r"^help:"))
    app.add_handler(CallbackQueryHandler(promote_cb, pattern=r"^pr:"))
    app.add_handler(CallbackQueryHandler(demote_cb, pattern=r"^dm:"))
    app.add_error_handler(on_error)

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
