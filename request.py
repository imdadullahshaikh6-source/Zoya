"""Join-request plugin: /request enable|on , /request disable|off  (or .request).

When enabled in a group, every new join request is announced in the chat with
the requester's name inside a quote block and two rich buttons:
    Accept  |  Decline
Only admins with the 'Invite Users' power (or the owner) can press them.
"""
import os

from motor.motor_asyncio import AsyncIOMotorClient
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import CallbackQueryHandler, ChatJoinRequestHandler, ContextTypes

from admin import _edit_rich_message, _fancy, _send_rich_message
from common import B, dual_command, esc

# Telegram's official Anonymous Admin Bot ID
ANON_ADMIN_ID = 1087968824

HELP_TXT = (
    "<b>✦ join request</b>\n\n"
    "/request enable (or .request on) — new join requests will be announced "
    "in the group with the user's name and Accept / Decline buttons\n"
    "/request disable (or .request off) — stop the notifications\n"
    "/request — show current status\n\n"
    "<b>requirements:</b>\n"
    "➤ admin with 'Invite Users' permission\n"
    "➤ i must be admin with 'Invite Users' too\n"
    "➤ group must have 'approve new members' (join requests) turned on"
)

COMMANDS = [("request", "Join request notifications on/off")]

ON_WORDS = {"enable", "on", "yes", "true"}
OFF_WORDS = {"disable", "off", "no", "false"}

# ───────────────────── DATABASE (self-contained) ─────────────────────
_col = None
_cache: dict = {}  # chat_id -> bool


def _collection():
    global _col
    if _col is None:
        client = AsyncIOMotorClient(os.environ["MONGO_URI"])
        _col = client[os.getenv("DB_NAME", "zoya_bot")]["join_request_settings"]
    return _col


async def _is_enabled(chat_id: int) -> bool:
    if chat_id in _cache:
        return _cache[chat_id]
    try:
        doc = await _collection().find_one({"chat_id": chat_id})
    except Exception as e:
        print(f"[request] db read failed: {e}")
        return False
    val = bool(doc and doc.get("enabled"))
    _cache[chat_id] = val
    return val


async def _set_enabled(chat_id: int, value: bool) -> None:
    _cache[chat_id] = value
    try:
        await _collection().update_one(
            {"chat_id": chat_id}, {"$set": {"enabled": value}}, upsert=True
        )
    except Exception as e:
        print(f"[request] db write failed: {e}")


# ───────────────────── HELPERS ─────────────────────
async def _can_invite(ctx, chat_id: int, user_id: int) -> bool:
    """Owner, or admin with 'Invite Users' power."""
    if user_id == ANON_ADMIN_ID:
        return True  # anonymous admin = an admin of this group
    try:
        m = await ctx.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    if m.status == ChatMemberStatus.OWNER:
        return True
    return m.status == ChatMemberStatus.ADMIN and bool(getattr(m, "can_invite_users", False))


def _request_panel(user) -> dict:
    """Rich message: name in a quote block + Accept / Decline buttons."""
    uname = f"@{user.username}" if user.username else "—"
    text = (
        f"{_fancy('New Join Request')}\n"
        f"{_fancy('Name')}: {user.full_name}\n"
        f"{_fancy('Username')}: {uname}\n"
        f"{_fancy('ID')}: {user.id}"
    )
    return {
        "blocks": [
            {"type": "blockquote", "blocks": [{"type": "paragraph", "text": text}]},
            {
                "type": "buttons",
                "buttons": [
                    {"text": _fancy("Accept"), "callback_data": f"rq:a:{user.id}", "style": "success"},
                    {"text": _fancy("Decline"), "callback_data": f"rq:d:{user.id}", "style": "danger"},
                ],
            },
        ]
    }


def _result_panel(user_name: str, accepted: bool, admin_name: str) -> dict:
    verb = "Accepted" if accepted else "Declined"
    icon = "✅" if accepted else "❌"
    text = f"{icon} {_fancy(verb)}\n{_fancy('User')}: {user_name}\n{_fancy('By')}: {admin_name}"
    return {"blocks": [{"type": "blockquote", "blocks": [{"type": "paragraph", "text": text}]}]}


# ───────────────────── JOIN REQUEST EVENT ─────────────────────
async def on_join_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if not req:
        return
    chat, user = req.chat, req.from_user
    if user.is_bot or not await _is_enabled(chat.id):
        return
    try:
        await _send_rich_message(ctx.bot, chat.id, _request_panel(user))
    except Exception as e:
        # fallback: normal message with normal inline buttons
        print(f"[request] rich send failed, using fallback: {e}")
        kb = InlineKeyboardMarkup([[
            B("𝘼𝙘𝙘𝙚𝙥𝙩", f"rq:a:{user.id}", style="success"),
            B("𝘿𝙚𝙘𝙡𝙞𝙣𝙚", f"rq:d:{user.id}", style="danger"),
        ]])
        uname = f"@{esc(user.username)}" if user.username else "—"
        txt = (
            f"<blockquote><b>New Join Request</b>\n"
            f"Name: {esc(user.full_name)}\nUsername: {uname}\nID: <code>{user.id}</code></blockquote>"
        )
        try:
            await ctx.bot.send_message(chat.id, txt, parse_mode=ParseMode.HTML, reply_markup=kb)
        except TelegramError as e2:
            print(f"[request] fallback send failed: {e2}")


# ───────────────────── ACCEPT / DECLINE BUTTONS ─────────────────────
async def request_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    qy = update.callback_query
    try:
        _, action, uid = qy.data.split(":")
        target_id = int(uid)
    except ValueError:
        await qy.answer()
        return

    msg = qy.message
    chat_id = msg.chat.id
    admin = qy.from_user

    if not await _can_invite(ctx, chat_id, admin.id):
        await qy.answer("Only admins with 'Invite Users' power can use these buttons.", show_alert=True)
        return

    accept = action == "a"
    try:
        if accept:
            await ctx.bot.approve_chat_join_request(chat_id, target_id)
        else:
            await ctx.bot.decline_chat_join_request(chat_id, target_id)
    except BadRequest as e:
        err = str(e).lower()
        if "already" in err or "hide_requester_missing" in err or "not found" in err:
            await qy.answer("This request was already handled or cancelled by the user.", show_alert=True)
            try:
                await ctx.bot.delete_message(chat_id, msg.message_id)
            except TelegramError:
                pass
        else:
            await qy.answer("I need the 'Invite Users' admin power to do this.", show_alert=True)
        return
    except TelegramError:
        await qy.answer("Something went wrong, try again.", show_alert=True)
        return

    await qy.answer("Accepted ✅" if accept else "Declined ❌")

    # find user's name from the original panel text (fallback: id)
    name = str(target_id)
    try:
        chat_member = await ctx.bot.get_chat_member(chat_id, target_id)
        name = chat_member.user.full_name
    except TelegramError:
        pass
    try:
        await _edit_rich_message(
            ctx.bot, chat_id, msg.message_id,
            _result_panel(name, accept, admin.full_name),
        )
    except Exception:
        try:
            await qy.edit_message_text(
                f"<blockquote>{'✅ Accepted' if accept else '❌ Declined'} — {esc(name)}\n"
                f"By: {esc(admin.full_name)}</blockquote>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass


# ───────────────────── /request COMMAND ─────────────────────
async def request_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg:
        return

    async def reply(text: str):
        await ctx.bot.send_message(
            chat.id, f"<blockquote>{text}</blockquote>",
            parse_mode=ParseMode.HTML, reply_to_message_id=msg.message_id,
        )

    if chat.type == ChatType.PRIVATE:
        await reply("𝙧𝙚𝙦𝙪𝙚𝙨𝙩 𝙘𝙤𝙢𝙢𝙖𝙣𝙙 𝙬𝙤𝙧𝙠𝙨 𝙞𝙣𝙨𝙞𝙙𝙚 𝙜𝙧𝙤𝙪𝙥𝙨 𝙤𝙣𝙡𝙮.")
        return

    if msg.sender_chat and not user:
        return
    if not await _can_invite(ctx, chat.id, user.id):
        await reply("𝙮𝙤𝙪 𝙣𝙚𝙚𝙙 𝙖𝙙𝙢𝙞𝙣 𝙬𝙞𝙩𝙝 <b>Invite Users</b> 𝙥𝙤𝙬𝙚𝙧 𝙩𝙤 𝙪𝙨𝙚 𝙩𝙝𝙞𝙨.")
        return

    # works for both "/request on" and ".request on"
    parts = (msg.text or "").split()
    arg = parts[1].lower() if len(parts) > 1 else ""

    if arg in ON_WORDS:
        bm = None
        try:
            bm = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
        except TelegramError:
            pass
        if not bm or bm.status != ChatMemberStatus.ADMIN or not getattr(bm, "can_invite_users", False):
            await reply("𝙞 𝙢𝙪𝙨𝙩 𝙗𝙚 𝙖𝙙𝙢𝙞𝙣 𝙬𝙞𝙩𝙝 <b>Invite Users</b> 𝙥𝙤𝙬𝙚𝙧 𝙛𝙞𝙧𝙨𝙩, 𝙩𝙝𝙚𝙣 𝙚𝙣𝙖𝙗𝙡𝙚 𝙖𝙜𝙖𝙞𝙣.")
            return
        await _set_enabled(chat.id, True)
        await reply("✅ <b>𝙅𝙤𝙞𝙣 𝙧𝙚𝙦𝙪𝙚𝙨𝙩 𝙣𝙤𝙩𝙞𝙛𝙞𝙘𝙖𝙩𝙞𝙤𝙣 𝙚𝙣𝙖𝙗𝙡𝙚𝙙.</b>")
    elif arg in OFF_WORDS:
        await _set_enabled(chat.id, False)
        await reply("❌ <b>𝙅𝙤𝙞𝙣 𝙧𝙚𝙦𝙪𝙚𝙨𝙩 𝙣𝙤𝙩𝙞𝙛𝙞𝙘𝙖𝙩𝙞𝙤𝙣 𝙙𝙞𝙨𝙖𝙗𝙡𝙚𝙙.</b>")
    else:
        state = "𝙀𝙣𝙖𝙗𝙡𝙚𝙙 ✅" if await _is_enabled(chat.id) else "𝘿𝙞𝙨𝙖𝙗𝙡𝙚𝙙 ❌"
        await reply(
            f"<b>𝙅𝙤𝙞𝙣 𝙧𝙚𝙦𝙪𝙚𝙨𝙩:</b> {state}\n\n"
            "/request enable | on\n/request disable | off"
        )


def register(app):
    dual_command(app, "request", request_cmd)
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(CallbackQueryHandler(request_cb, pattern=r"^rq:"))
  
