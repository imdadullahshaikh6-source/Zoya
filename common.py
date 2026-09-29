"""Shared helpers used by every plugin: styling, buttons, permissions, dot-commands."""
import html
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import CommandHandler, MessageHandler, filters

import database as dbase

log = logging.getLogger("bot")
OWNER, ADMIN = ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR

# ───────────────────── FONT / STYLE ─────────────────────
_SC = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))
assert len(_SC) == 26
# tags, {placeholders}, /commands, .commands and @names are never converted
_SKIP = re.compile(r"(<[^>]+>|\{[^}]*\}|(?<![\w./])[./][a-zA-Z_]+|@\w+)")


def sc(text: str) -> str:
    parts = _SKIP.split(text)
    return "".join(
        p if i % 2 else "".join(_SC.get(ch, ch) for ch in p.lower())
        for i, p in enumerate(parts)
    )


def T(text: str, **kw) -> str:
    """Small-caps static text, then fill {placeholders} with pre-escaped values."""
    return sc(text).format(**kw)


def esc(t) -> str:
    return html.escape(str(t), quote=False)


def q(text: str) -> str:
    """Every bot message goes inside a quote block."""
    return f"<blockquote>{text}</blockquote>"


def mention(u) -> str:
    name = getattr(u, "full_name", None) or str(u.id)
    return f'<a href="tg://user?id={u.id}">{esc(name)}</a>'


def B(text, data=None, url=None, style=None) -> InlineKeyboardButton:
    """style: 'primary' (blue) | 'success' (green) | 'danger' (red)."""
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


# ───────────────────── ROSE-STYLE BUTTON PARSER ─────────────────────
BTN_RE = re.compile(r"\[([^\]]+)\]\(buttonurl://([^)]+)\)", re.I)


def parse_buttons(raw: str):
    """
    [Label](buttonurl://https://example.com)               -> own row
    [Label](buttonurl://https://example.com:same)           -> joins previous row
    [Label](buttonurl://https://example.com:danger)         -> red button
    [Label](buttonurl://https://example.com:same:success)   -> both
    Returns (clean_text_without_button_lines, InlineKeyboardMarkup|None)
    """
    if not raw:
        return raw, None
    rows = []
    for m in BTN_RE.finditer(raw):
        label, tail = m.group(1), m.group(2)
        parts = tail.split(":")
        same, color = False, None
        while len(parts) > 1 and parts[-1].lower() in ("same", "primary", "success", "danger"):
            tok = parts.pop().lower()
            if tok == "same":
                same = True
            else:
                color = tok
        url = ":".join(parts)
        btn = B(label, url=url, style=color)
        if same and rows:
            rows[-1].append(btn)
        else:
            rows.append([btn])
    clean = BTN_RE.sub("", raw)
    clean = re.sub(r"[ \t]*\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", clean).strip()
    return clean, (InlineKeyboardMarkup(rows) if rows else None)


# ───────────────────── PERMISSIONS ─────────────────────
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


async def require_admin(update, ctx, right=None) -> bool:
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
            ctx, chat.id,
            T("{m}, you don't have the {l} right.", m=mention(user), l=f"<b>{RL[right]}</b>"),
            reply_to=msg.message_id,
        )
        return False
    return True


async def tag_missing(ctx, chat_id, user, labels, reply_to=None):
    await say(
        ctx, chat_id,
        T(
            "{m}, i don't have the {l} power. please give it to me and try again.",
            m=mention(user), l="<b>" + esc(", ".join(labels)) + "</b>",
        ),
        reply_to=reply_to,
    )


def explain(e: Exception) -> str:
    s = str(e).lower()
    if "not enough rights" in s or "chat_admin_required" in s or "right_forbidden" in s:
        return "I don't have enough rights for this."
    if "user_creator" in s or "chat owner" in s or "can't remove chat owner" in s:
        return "This user is the group owner. Nothing can be changed."
    if "user_not_participant" in s or "participant_id_invalid" in s:
        return "This user is not in the group."
    return f"Telegram said: {esc(e)}"


# ───────────────────── TARGET RESOLUTION (reply / @username / id) ─────────────────────
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
                cchat = await ctx.bot.get_chat(a)
                if cchat.type == ChatType.PRIVATE:
                    uid = cchat.id
            except TelegramError:
                pass
    if uid is None:
        return None, ""
    m = await get_member(ctx, chat.id, uid)
    return (m.user if m else None), title


# ───────────────────── DUAL /command AND .command SUPPORT ─────────────────────
def _dotify(callback):
    async def wrapper(update, ctx):
        msg = update.effective_message
        text = msg.text or msg.caption or ""
        ctx.args = text.split()[1:]
        await callback(update, ctx)
    return wrapper


def dual_command(app, names, callback, group_only=True):
    """Registers callback for /name (and .name) — .name works alongside / everywhere it's added."""
    if isinstance(names, str):
        names = [names]
    f = filters.ChatType.GROUPS if group_only else filters.ALL
    app.add_handler(CommandHandler(names, callback, filters=f))
    pattern = re.compile(
        r"^\.(?:%s)(?:@\w+)?(?:\s|$)" % "|".join(re.escape(n) for n in names), re.I
    )
    app.add_handler(MessageHandler(filters.Regex(pattern) & f, _dotify(callback)))


def human_delta(seconds: float) -> str:
    seconds = int(seconds)
    d, seconds = divmod(seconds, 86400)
    h, seconds = divmod(seconds, 3600)
    m, seconds = divmod(seconds, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts[:2])
  
