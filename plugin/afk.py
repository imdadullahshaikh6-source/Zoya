"""AFK plugin: /afk, .afk, or a plain 'off ...' message all set AFK.
Speaking again clears it; replying/mentioning an AFK user tells you they're away."""
import re
import time

from telegram.constants import ChatType
from telegram.ext import MessageHandler, filters

import database as dbase
from common import T, dual_command, human_delta, mention, say

HELP_TXT = (
    "<b>✦ afk — away from keyboard</b>\n\n"
    "/afk (or .afk) [reason] — mark yourself afk\n"
    "just typing <code>off</code> or <code>off &lt;reason&gt;</code> also works, e.g. "
    "<code>off goodnight</code>\n\n"
    "send any other message to come back automatically. if someone replies to you or "
    "@mentions you while you're afk, i tell them you're away and for how long."
)

COMMANDS = [("afk", "Mark yourself as AFK")]

_OFF_RE = re.compile(r"^off\b(.*)$", re.I)


async def afk_cmd(update, ctx):
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    reason = " ".join(ctx.args).strip() if ctx.args else ""
    await dbase.afk_set(user.id, reason, update.effective_chat.id)
    extra = T(" reason: {r}", r=reason) if reason else ""
    await say(
        ctx, update.effective_chat.id,
        T("{m} is now afk.", m=mention(user)) + extra,
        reply_to=msg.message_id,
    )


async def off_text(update, ctx):
    msg, user = update.effective_message, update.effective_user
    if not msg or not user or update.effective_chat.type == ChatType.PRIVATE:
        return
    m = _OFF_RE.match((msg.text or "").strip())
    if not m:
        return
    reason = m.group(1).strip()
    await dbase.afk_set(user.id, reason, update.effective_chat.id)
    extra = T(" reason: {r}", r=reason) if reason else ""
    await say(ctx, update.effective_chat.id, T("{m} is now afk.", m=mention(user)) + extra, reply_to=msg.message_id)


async def watch(update, ctx):
    """Clears AFK on any message from an AFK user; notices mentions/replies to AFK users."""
    msg = update.effective_message
    if not msg:
        return
    chat = update.effective_chat
    user = update.effective_user
    text = msg.text or msg.caption or ""

    if user and not user.is_bot and chat.type != ChatType.PRIVATE:
        rec = await dbase.afk_get(user.id)
        if rec and not _OFF_RE.match(text.strip()) and not re.match(r"^[/.]afk\b", text, re.I):
            await dbase.afk_clear(user.id)
            await say(
                ctx, chat.id,
                T("{m} is back. was afk for {d}.", m=mention(user), d=human_delta(time.time() - rec["since"])),
                reply_to=msg.message_id,
            )

    targets = {}
    rep = msg.reply_to_message
    if rep and rep.from_user and not rep.from_user.is_bot:
        targets[rep.from_user.id] = rep.from_user
    for ent in msg.entities or []:
        if ent.type == "text_mention" and ent.user and not ent.user.is_bot:
            targets[ent.user.id] = ent.user
        elif ent.type == "mention":
            uname = text[ent.offset: ent.offset + ent.length].lstrip("@")
            uid = await dbase.get_user_id_by_username(uname)
            if uid and uid not in targets:
                targets[uid] = None
    for uid, uobj in targets.items():
        if user and uid == user.id:
            continue
        rec = await dbase.afk_get(uid)
        if not rec:
            continue
        name = mention(uobj) if uobj else f'<a href="tg://user?id={uid}">this user</a>'
        extra = T(" reason: {r}", r=rec["reason"]) if rec.get("reason") else ""
        await say(
            ctx, chat.id,
            T("{m} is afk (since {d} ago).", m=name, d=human_delta(time.time() - rec["since"])) + extra,
            reply_to=msg.message_id,
        )


def register(app):
    dual_command(app, "afk", afk_cmd)
    grp = filters.ChatType.GROUPS
    app.add_handler(MessageHandler(filters.Regex(_OFF_RE) & grp & filters.TEXT, off_text))
    app.add_handler(MessageHandler(grp & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, watch), group=1)
  
