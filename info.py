"""Info plugin: /id, .id, /info, .info -> user info card (Rose style).

Reply to anyone and send the command to get THEIR info.
Without a reply: your own info. Also works with @username / user id / text mention.
The ID is wrapped in <code> so one tap copies it.
"""
import logging
import re

from telegram import MessageEntity, ReplyParameters
from telegram.constants import ParseMode

import database as dbase
from common import dual_command, esc

log = logging.getLogger("info")

COMMANDS = [
    ("id", "Get a user's ID and info"),
    ("info", "Get a user's info"),
]

HELP_TXT = (
    "<b>🆔 𝙄𝙣𝙛𝙤</b>\n\n"
    "Get the ID and details of any user.\n\n"
    "<b>Commands:</b>\n"
    "• <code>/id</code> (or <code>.id</code>) — show info of yourself.\n"
    "• <code>/id</code> (reply) — show info of the replied user.\n"
    "• <code>/info</code> (or <code>.info</code>) — same as /id.\n"
    "• <code>/info @username</code> or <code>/info user_id</code> — look up a user.\n\n"
    "<i>Tap the ID to copy it.</i>"
)


async def _from_db(chat_id: int, arg: str):
    """Look a member up in the bot's own members collection."""
    try:
        if arg.isdigit():
            return await dbase.db.members.find_one({"chat_id": chat_id, "user_id": int(arg)})
        uname = arg.lstrip("@")
        if not uname:
            return None
        return await dbase.db.members.find_one({
            "chat_id": chat_id,
            "username": {"$regex": f"^{re.escape(uname)}$", "$options": "i"},
        })
    except Exception as e:
        log.warning("members lookup failed for %r: %r", arg, e)
        return None


async def _resolve(update, ctx):
    """Return (user_id, first_name, last_name, username) or None if not found."""
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user

    # 1. reply -> that user  (ignore the invisible "topic root" reply in forum groups)
    rep = msg.reply_to_message
    if rep and msg.is_topic_message and rep.message_id == msg.message_thread_id:
        rep = None
    if rep and rep.from_user:
        u = rep.from_user
        return u.id, u.first_name, u.last_name, u.username

    # 2. text mention (user without username that was tagged from the member list)
    for ent in (msg.entities or []):
        if ent.type == MessageEntity.TEXT_MENTION and ent.user:
            u = ent.user
            return u.id, u.first_name, u.last_name, u.username

    # 3. argument: @username or numeric id
    parts = (msg.text or msg.caption or "").split()
    if len(parts) >= 2:
        arg = parts[1].strip()
        doc = await _from_db(chat.id, arg)
        uid = (doc or {}).get("user_id") or (int(arg) if arg.isdigit() else None)

        for target in ([uid] if uid else []) + ([arg] if arg.startswith("@") else []):
            try:
                c = await ctx.bot.get_chat(target)
                return c.id, c.first_name or c.title, getattr(c, "last_name", None), c.username
            except Exception as e:
                log.info("get_chat(%r) failed: %r", target, e)

        if doc and doc.get("user_id"):
            return doc["user_id"], doc.get("name"), None, doc.get("username")
        if uid:
            return uid, None, None, None
        return None

    # 4. nothing given -> yourself
    return user.id, user.first_name, user.last_name, user.username


def _card(uid: int, first, last, username) -> str:
    lines = [
        "User info:",
        f"ID: <code>{uid}</code>",
        f"First Name: {esc(first) if first else 'Unknown'}",
    ]
    if last:
        lines.append(f"Last Name: {esc(last)}")
    if username:
        lines.append(f"Username: @{esc(username)}")
    lines.append(f'User link: <a href="tg://user?id={uid}">link</a>')
    return "\n".join(lines)


async def info_cmd(update, ctx):
    msg, chat = update.effective_message, update.effective_chat
    reply_params = ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True)

    target = await _resolve(update, ctx)
    if not target:
        await ctx.bot.send_message(
            chat.id,
            "I couldn't find that user. Reply to their message or use their ID.",
            parse_mode=ParseMode.HTML, reply_parameters=reply_params,
        )
        return

    uid, first, last, username = target
    await ctx.bot.send_message(
        chat.id, _card(uid, first, last, username),
        parse_mode=ParseMode.HTML, reply_parameters=reply_params,
    )


def register(app):
    dual_command(app, "id", info_cmd, group_only=False)
    dual_command(app, "info", info_cmd, group_only=False)
