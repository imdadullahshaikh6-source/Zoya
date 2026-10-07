"""Log Channel — sends group activity logs to a linked channel."""
import asyncio
import html
import logging
import time

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    MessageOriginChannel,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, ContextTypes, MessageHandler, filters,
    ChatMemberHandler, ChatJoinRequestHandler,
)

import database as dbase
from common import B, T, dual_command, say

log = logging.getLogger("logchannel")


# ─────────────────────────── HELP TEXT ───────────────────────────
HELP_TXT = (
    "📋 <b>Log Channel</b>\n\n"
    "Automatically log all group activity to a linked channel.\n\n"
    "<b>Setup (run in your log channel):</b>\n"
    "1. Add me to your log channel as admin (with post rights)\n"
    "2. Run <code>/setlog</code> (or <code>.setlog</code>) in the channel\n"
    "3. I'll reply with a message — <b>forward it to the group</b> you want to log\n"
    "4. Done! That group's activity will now be sent to the channel\n\n"
    "<b>Commands:</b>\n"
    "• <code>/setlog</code> (or <code>.setlog</code>) — setup (run in channel)\n"
    "• <code>/unsetlog</code> (or <code>.unsetlog</code>) — remove log link\n"
    "• <code>/logchannel</code> (or <code>.logchannel</code>) — show current channel\n\n"
    "<b>Logged events:</b>\n"
    "• Ban, Unban, Mute, Unmute, Kick, Warn\n"
    "• Promote, Demote\n"
    "• Pin\n"
    "• Locks (lock/unlock)\n"
    "• Group photo / title change\n"
    "• Join requests, Approvals\n"
    "• Joins, Leaves\n\n"
    "<i>Only admins can use these commands.</i>"
)

COMMANDS = [
    ("setlog", "Setup log channel (run in channel)"),
    ("unsetlog", "Remove log channel link"),
    ("logchannel", "Show current log channel"),
]


# ─────────────────────────── STATE ───────────────────────────
_pending_setup: dict = {}


def _cleanup_pending():
    now = time.time()
    for k in list(_pending_setup.keys()):
        if now - _pending_setup[k] > 1800:
            _pending_setup.pop(k, None)


# ─────────────────────────── HELPERS ───────────────────────────
def _esc(s) -> str:
    if s is None:
        return ""
    return html.escape(str(s))


def _user_line(label: str, uid, uname) -> str:
    name = _esc(uname) if uname else "Unknown"
    return f"<b>{label}:</b> {name} (ID: {uid})"


def _chat_line(chat) -> str:
    return f"<b>Chat:</b> {_esc(chat.title) if chat.title else 'Unknown'}"


def _msg_link(chat, message_id) -> str | None:
    """Build a t.me message link for the given chat + message id.
    Returns None if chat type can't produce a link (e.g. basic groups)."""
    if not chat or not message_id:
        return None
    try:
        # Public supergroup / channel with username
        if getattr(chat, "username", None):
            return f"https://t.me/{chat.username}/{message_id}"
        # Private supergroup: chat_id = -100XXXXXXXXXX
        cid = str(chat.id)
        if cid.startswith("-100"):
            short = cid[4:]
            return f"https://t.me/c/{short}/{message_id}"
    except Exception:
        pass
    return None


def _msg_link_for_chat_id(chat_id: int, message_id: int, username: str | None = None) -> str | None:
    """Build a t.me message link using raw chat_id when we don't have a Chat object."""
    if not chat_id or not message_id:
        return None
    try:
        if username:
            return f"https://t.me/{username}/{message_id}"
        cid = str(chat_id)
        if cid.startswith("-100"):
            short = cid[4:]
            return f"https://t.me/c/{short}/{message_id}"
    except Exception:
        pass
    return None


async def send_log(bot, chat_id: int, text: str):
    try:
        cfg = await dbase.logchannel_get(chat_id)
    except Exception as e:
        log.warning("[logchannel] db read failed for %s: %s", chat_id, e)
        return
    if not cfg or not cfg.get("channel_id"):
        return
    channel_id = cfg["channel_id"]
    try:
        await bot.send_message(
            channel_id,
            f"<blockquote>{text}</blockquote>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramError as e:
        log.warning("[logchannel] send to %s failed: %s", channel_id, e)


async def log_action(
    bot, chat_id: int, action: str, *,
    chat_title: str = "",
    admin_id=None, admin_name=None,
    user_id=None, user_name=None,
    reason: str = "",
    message_link: str = "",
    extra_lines: list | None = None,
    footer: str = "",
):
    lines = [f"<b>#{action.upper()}:</b>"]
    if chat_title:
        lines.append(f"<b>Chat:</b> {_esc(chat_title)}")
    elif user_id is not None or admin_id is not None:
        lines.append("<b>Chat:</b> <i>Unknown</i>")
    if admin_id is not None:
        lines.append(_user_line("Admin", admin_id, admin_name))
    if user_id is not None:
        lines.append(_user_line("User", user_id, user_name))
    if message_link:
        lines.append(f'<b>Message link:</b> <a href="{_esc(message_link)}">link</a>')
    if reason:
        lines.append(f"<b>Reason:</b> {_esc(reason)}")
    if extra_lines:
        lines.extend(extra_lines)
    if footer:
        lines.append(_esc(footer))
    await send_log(bot, chat_id, "\n".join(lines))


# ─────────────────────────── COMMAND HANDLERS ───────────────────────────
async def setlog_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat:
        return

    if chat.type == ChatType.CHANNEL:
        try:
            bm = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
        except TelegramError:
            return
        if bm.status != ChatMemberStatus.ADMINISTRATOR:
            await ctx.bot.send_message(
                chat.id,
                "⚠️ Make me an admin here first (with post rights), then try again.",
            )
            return

        setup_msg = await ctx.bot.send_message(
            chat.id,
            "<b>🔗 Log Channel Setup</b>\n\n"
            "Forward this message to the group where you want logs.",
            parse_mode=ParseMode.HTML,
        )
        _cleanup_pending()
        _pending_setup[(chat.id, setup_msg.message_id)] = time.time()
        return

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await say(
            ctx, chat.id,
            T("⚠️ Run <code>/setlog</code> in your <b>log channel</b> "
              "(add me as admin there first), then forward my reply to this group."),
            reply_to=msg.message_id,
        )
        return


async def unsetlog_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat:
        return

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        try:
            m = await ctx.bot.get_chat_member(chat.id, update.effective_user.id)
        except TelegramError:
            return
        if m.status not in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
            await say(ctx, chat.id, T("⚠️ Only admins can use this."), reply_to=msg.message_id)
            return

        removed = await dbase.logchannel_unset(chat.id)
        if removed:
            await say(ctx, chat.id, T("✅ Log channel removed for this group."), reply_to=msg.message_id)
        else:
            await say(ctx, chat.id, T("ℹ️ No log channel set for this group."), reply_to=msg.message_id)
        return

    if chat.type == ChatType.CHANNEL:
        n = await dbase.logchannel_unset_by_channel(chat.id)
        await ctx.bot.send_message(
            chat.id,
            f"✅ Removed log setup for <b>{n}</b> group(s).",
            parse_mode=ParseMode.HTML,
        )
        return


async def logchannel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat:
        return

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        cfg = await dbase.logchannel_get(chat.id)
        if not cfg or not cfg.get("channel_id"):
            await say(
                ctx, chat.id,
                T("ℹ️ No log channel set for this group.\n"
                  "Run <code>/setlog</code> in a channel to set one up."),
                reply_to=msg.message_id,
            )
            return
        ch_name = cfg.get("channel_name") or "Unknown"
        ch_id = cfg.get("channel_id")
        await say(
            ctx, chat.id,
            T(f"📋 <b>Log Channel</b>\n\n"
              f"Group: <b>{_esc(chat.title or 'this group')}</b>\n"
              f"Channel: <b>{_esc(ch_name)}</b>\n"
              f"Channel ID: <code>{ch_id}</code>"),
            reply_to=msg.message_id,
        )
        return

    if chat.type == ChatType.CHANNEL:
        groups = await dbase.logchannel_groups_for_channel(chat.id)
        if not groups:
            await ctx.bot.send_message(
                chat.id,
                "ℹ️ No groups are linked to this channel.",
            )
            return
        lines = ["📋 <b>Groups linked to this channel:</b>", ""]
        for g in groups:
            lines.append(
                f"• {_esc(g.get('chat_title') or 'Unknown')} (<code>{g['chat_id']}</code>)"
            )
        await ctx.bot.send_message(
            chat.id, "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )
        return


# ─────────────────────────── AUTO-LOGGERS ───────────────────────────
async def _service_logger(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    cfg = await dbase.logchannel_get(chat.id)
    if not cfg or not cfg.get("channel_id"):
        return

    # ── PIN: embed link of the pinned message inside the word "pinned" ──
    if msg.pinned_message:
        actor = update.effective_user
        pinned_id = msg.pinned_message.message_id
        link = _msg_link(chat, pinned_id)
        lines = [f"<b>#PIN:</b>", _chat_line(chat)]
        if actor and not actor.is_bot:
            lines.append(_user_line("Admin", actor.id, actor.full_name))
        if link:
            lines.append(f'A new message has been <a href="{link}">pinned</a>.')
        else:
            lines.append("A new message has been pinned.")
        await send_log(ctx.bot, chat.id, "\n".join(lines))
        return

    # ── PHOTO / BACKGROUND ──
    if msg.new_chat_photo or msg.delete_chat_photo or msg.chat_background_set:
        actor = update.effective_user
        lines = [f"<b>#PHOTO:</b>", _chat_line(chat)]
        if actor and not actor.is_bot:
            lines.append(_user_line("Admin", actor.id, actor.full_name))
        if msg.new_chat_photo:
            lines.append("Chat photo was changed.")
        elif msg.delete_chat_photo:
            lines.append("Chat photo was removed.")
        else:
            lines.append("Chat background was changed.")
        await send_log(ctx.bot, chat.id, "\n".join(lines))
        return

    # ── TITLE ──
    if msg.new_chat_title:
        actor = update.effective_user
        lines = [
            f"<b>#TITLE:</b>",
            _chat_line(chat),
            f"<b>New title:</b> {_esc(msg.new_chat_title)}",
        ]
        if actor and not actor.is_bot:
            lines.append(_user_line("Admin", actor.id, actor.full_name))
        await send_log(ctx.bot, chat.id, "\n".join(lines))
        return

    # ── LEAVE ──
    if msg.left_chat_member:
        u = msg.left_chat_member
        lines = [
            f"<b>#LEAVE:</b>",
            _chat_line(chat),
            _user_line("User", u.id, u.full_name),
            f"{_esc(u.full_name)} left the chat.",
        ]
        await send_log(ctx.bot, chat.id, "\n".join(lines))
        return


async def _chat_member_logger(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if not cmu:
        return
    chat = cmu.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    cfg = await dbase.logchannel_get(chat.id)
    if not cfg or not cfg.get("channel_id"):
        return

    actor = cmu.from_user
    if actor and actor.id == ctx.bot.id:
        return

    old = cmu.old_chat_member
    new = cmu.new_chat_member
    target = new.user

    admin_id = actor.id if actor else None
    admin_name = actor.full_name if actor else None
    user_id = target.id
    user_name = target.full_name

    lines = None

    old_s = old.status
    new_s = new.status

    # ── BAN ──
    if new_s == ChatMemberStatus.BANNED and old_s != ChatMemberStatus.BANNED:
        lines = [f"<b>#BAN:</b>", _chat_line(chat)]
        if admin_id: lines.append(_user_line("Admin", admin_id, admin_name))
        lines.append(_user_line("User", user_id, user_name))
        lines.append("Reason:")

    # ── UNBAN ──
    elif old_s == ChatMemberStatus.BANNED and new_s != ChatMemberStatus.BANNED:
        lines = [f"<b>#UNBAN:</b>", _chat_line(chat)]
        if admin_id: lines.append(_user_line("Admin", admin_id, admin_name))
        lines.append(_user_line("User", user_id, user_name))
        lines.append("Reason:")

    # ── PROMOTE ──
    elif old_s in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED) and new_s == ChatMemberStatus.ADMINISTRATOR:
        lines = [f"<b>#PROMOTE:</b>", _chat_line(chat)]
        if admin_id: lines.append(_user_line("Admin", admin_id, admin_name))
        lines.append(_user_line("User", user_id, user_name))
        lines.append(f"{_esc(user_name)} was promoted to admin.")

    # ── DEMOTE ──
    elif old_s == ChatMemberStatus.ADMINISTRATOR and new_s in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
        lines = [f"<b>#DEMOTE:</b>", _chat_line(chat)]
        if admin_id: lines.append(_user_line("Admin", admin_id, admin_name))
        lines.append(_user_line("User", user_id, user_name))
        lines.append(f"{_esc(user_name)} was demoted.")

    # ── MUTE ──
    elif new_s == ChatMemberStatus.RESTRICTED and not getattr(new, "can_send_messages", True) and old_s != ChatMemberStatus.RESTRICTED:
        lines = [f"<b>#MUTE:</b>", _chat_line(chat)]
        if admin_id: lines.append(_user_line("Admin", admin_id, admin_name))
        lines.append(_user_line("User", user_id, user_name))
        lines.append("Reason:")

    # ── WELCOME (Join / Approve) ──
    elif new_s == ChatMemberStatus.MEMBER and old_s in (ChatMemberStatus.LEFT, ChatMemberStatus.RESTRICTED):
        lines = [f"<b>#WELCOME:</b>", _chat_line(chat)]
        if admin_id: lines.append(_user_line("Admin", admin_id, admin_name))
        lines.append(_user_line("User", user_id, user_name))
        if cmu.invite_link:
            if cmu.invite_link.creator:
                lines.append(_user_line("Invitelink from", cmu.invite_link.creator.id, cmu.invite_link.creator.full_name))
            lines.append(f"<b>Invitelink name:</b> {_esc(cmu.invite_link.name or 'None')}")
        if admin_id:
            lines.append(_user_line("Approved by", admin_id, admin_name))
        else:
            lines.append("User joined the chat.")

    if lines:
        await send_log(ctx.bot, chat.id, "\n".join(lines))


async def _join_request_logger(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if not req:
        return
    chat = req.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    cfg = await dbase.logchannel_get(chat.id)
    if not cfg or not cfg.get("channel_id"):
        return

    user = req.from_user
    lines = [
        f"<b>#JOINREQUEST:</b>",
        _chat_line(chat),
        _user_line("User", user.id, user.full_name),
    ]
    il = req.invite_link
    if il and il.creator:
        c = il.creator
        lines.append(_user_line("Invitelink from", c.id, c.full_name))
        lines.append(f"<b>Invitelink name:</b> {_esc(il.name or 'None')}")
    lines.append("User has requested to join the chat.")
    await send_log(ctx.bot, chat.id, "\n".join(lines))


async def _forward_detector(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    origin = msg.forward_origin
    if not isinstance(origin, MessageOriginChannel):
        return

    key = (origin.chat.id, origin.message_id)
    if key not in _pending_setup:
        return

    try:
        m = await ctx.bot.get_chat_member(chat.id, user.id)
    except TelegramError:
        return
    if m.status not in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
        await say(ctx, chat.id, T("⚠️ Only admins can set up the log channel."), reply_to=msg.message_id)
        return

    channel_id = origin.chat.id
    channel_name = origin.chat.title or "Log Channel"

    try:
        await dbase.logchannel_set(chat.id, channel_id, channel_name, user.id)
    except Exception as e:
        log.error("[logchannel] db write failed: %s", e)
        await say(ctx, chat.id, T("⚠️ Couldn't save. Try again."), reply_to=msg.message_id)
        return

    _pending_setup.pop(key, None)

    await say(
        ctx, chat.id,
        T(f"✅ <b>Log channel set!</b>\n\n"
          f"📢 All activity in <b>{_esc(chat.title or 'this group')}</b> "
          f"will now be logged to <b>{_esc(channel_name)}</b>."),
        reply_to=msg.message_id,
    )


# ─────────────────────────── REGISTER ───────────────────────────
def register(app: Application):
    dual_command(app, "setlog", setlog_cmd)
    dual_command(app, "unsetlog", unsetlog_cmd)
    dual_command(app, "logchannel", logchannel_cmd)

    app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST & filters.Regex(r"^[/\.]setlog(@\w+)?$"),
        setlog_cmd
    ), group=95)
    app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST & filters.Regex(r"^[/\.]unsetlog(@\w+)?$"),
        unsetlog_cmd
    ), group=95)
    app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST & filters.Regex(r"^[/\.]logchannel(@\w+)?$"),
        logchannel_cmd
    ), group=95)

    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.ALL & filters.ChatType.GROUPS,
            _service_logger,
        ),
        group=98,
    )
    app.add_handler(
        ChatMemberHandler(_chat_member_logger, ChatMemberHandler.CHAT_MEMBER),
        group=99,
    )
    app.add_handler(
        ChatJoinRequestHandler(_join_request_logger),
        group=99,
    )
    app.add_handler(
        MessageHandler(
            filters.FORWARDED & filters.ChatType.GROUPS,
            _forward_detector,
        ),
        group=96,
                                   )
