"""Fun plugin: .waifu (random waifu pic) and .couple (ships two group members)."""
import random

import httpx
from telegram import ReplyParameters
from telegram.constants import ChatType, ParseMode

import database as dbase
from common import T, dual_command, esc, mention, q, say

HELP_TXT = (
    "<b>✦ fun</b>\n\n"
    "/waifu (or .waifu) — get a random waifu picture\n"
    "/couple (or .couple) — ships two random members of the group for today"
)
COMMANDS = [("waifu", "Get a random waifu"), ("couple", "Ship two random members")]

WAIFU_API = "https://api.waifu.pics/sfw/waifu"


async def waifu_cmd(update, ctx):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(WAIFU_API)
            r.raise_for_status()
            url = r.json()["url"]
    except Exception:
        await say(ctx, chat.id, T("couldn't fetch a waifu right now, try again in a bit."), reply_to=msg.message_id)
        return
    cap = q(T("<b>{m}'s waifu for today ✨</b>", m=mention(user)))
    await ctx.bot.send_photo(
        chat.id, url, caption=cap, parse_mode=ParseMode.HTML,
        reply_parameters=ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True),
    )


async def couple_cmd(update, ctx):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        await say(ctx, chat.id, T("this only works inside groups."))
        return
    members = await dbase.random_members(chat.id, 8)
    if len(members) < 2:
        await say(ctx, chat.id, T("not enough active members seen yet — chat a bit more first!"), reply_to=msg.message_id)
        return
    a, b = random.sample(members, 2)
    pct = random.randint(40, 100)

    def link(m):
        return f'<a href="tg://user?id={m["user_id"]}">{esc(m.get("name") or "someone")}</a>'

    text = T("<b>💞 today's couple</b>\n\n{a} + {b}\nmatch: <b>{p}%</b>", a=link(a), b=link(b), p=pct)
    await say(ctx, chat.id, text, reply_to=msg.message_id)


def register(app):
    dual_command(app, "waifu", waifu_cmd, group_only=False)
    dual_command(app, "couple", couple_cmd, group_only=False)
  
