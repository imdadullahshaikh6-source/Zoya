"""Logs plugin: log bot /start events to a designated group (owner only).
Uses a local JSON file for state — MongoDB stays clean.
"""
import html
import json
import logging
import os
from datetime import datetime

from telegram import Update, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes, CallbackQueryHandler

from common import B, T, dual_command, say

log = logging.getLogger("logs")

BOT_OWNER_ID = 8373739674

# Local state file (persists on VPS)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs_state.json")

# Don't expose to public menu
COMMANDS = []


def _load_state() -> dict:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.warning("logs_state load failed: %s", e)
    return {"enabled": False, "group_id": None, "total_users": 0, "logged_users": []}


def _save_state(state: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.warning("logs_state save failed: %s", e)


async def startlogs_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if user.id != BOT_OWNER_ID:
        await say(ctx, chat.id, T("⚠️ only the bot owner can use this."), reply_to=msg.message_id)
        return

    # If in group, ensure bot is admin
    if chat.type != ChatType.PRIVATE:
        me = ctx.bot_data.get("me")
        try:
            member = await ctx.bot.get_chat_member(chat.id, me.id)
            if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                await say(ctx, chat.id, T("⚠️ please make me an admin here first."), reply_to=msg.message_id)
                return
        except TelegramError:
            pass

    kb = InlineKeyboardMarkup([
        [
            B("✅ 𝙎𝙩𝙖𝙧𝙩 𝙇𝙤𝙜𝙨", "logs:on", style="success"),
            B("🛑 𝙎𝙩𝙤𝙥 𝙇𝙤𝙜𝙨", "logs:off", style="danger"),
        ],
    ])

    state = _load_state()
    status = "🟢 ON" if state.get("enabled") else "🔴 OFF"
    current_group = state.get("group_id")
    total_users = state.get("total_users", 0)

    text = (
        f"<b>📋 𝙇𝙤𝙜𝙨 𝙎𝙚𝙩𝙪𝙥</b>\n\n"
        f"Current status: <b>{status}</b>\n"
        f"Log group: <code>{current_group or 'not set'}</code>\n"
        f"Total users who started: <b>{total_users}</b>\n\n"
        f"Tap <b>Start Logs</b> to set this chat (<code>{chat.id}</code>) as the logs destination.\n"
        f"Tap <b>Stop Logs</b> to disable logging."
    )
    await say(ctx, chat.id, T(text), kb=kb, reply_to=msg.message_id)


async def logs_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    qy = update.callback_query
    if qy.from_user.id != BOT_OWNER_ID:
        await qy.answer("❌ Only the bot owner can use this.", show_alert=True)
        return

    action = qy.data.split(":")[1]
    chat_id = qy.message.chat_id
    state = _load_state()

    if action == "on":
        state["enabled"] = True
        state["group_id"] = chat_id
        _save_state(state)
        await qy.answer("✅ Logs enabled")
        try:
            await qy.edit_message_text(
                T(
                    "<b>📋 𝙇𝙤𝙜𝙨 𝙀𝙣𝙖𝙗𝙡𝙚𝙙</b>\n\n"
                    f"All new /start events will be logged in this chat (<code>{chat_id}</code>)."
                ),
            )
        except TelegramError:
            pass
    else:
        state["enabled"] = False
        _save_state(state)
        await qy.answer("🛑 Logs disabled")
        try:
            await qy.edit_message_text(
                T("<b>📋 𝙇𝙤𝙜𝙨 𝘿𝙞𝙨𝙖𝙗𝙡𝙚𝙙</b>\n\nNew /start events won't be logged."),
            )
        except TelegramError:
            pass


async def log_user_start(ctx, user):
    """Called from start_cmd. Logs only if this is user's FIRST /start."""
    try:
        state = _load_state()
        if not state.get("enabled") or not state.get("group_id"):
            return
        group_id = int(state["group_id"])

        # ✅ Check if new user (only first time)
        logged = set(state.get("logged_users", []))
        if user.id in logged:
            return  # already logged before, skip

        # Add user and increment count
        logged.add(user.id)
        state["logged_users"] = list(logged)
        state["total_users"] = int(state.get("total_users", 0)) + 1
        _save_state(state)

        total = state["total_users"]
        uname = f"@{user.username}" if user.username else "no username"
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        text = (
            "<b>🆕 𝙉𝙚𝙬 𝙐𝙨𝙚𝙧 𝙎𝙩𝙖𝙧𝙩𝙚𝙙</b>\n"
            f"• Name: {html.escape(user.first_name or 'User')}\n"
            f"• ID: <code>{user.id}</code>\n"
            f"• Username: {html.escape(uname)}\n"
            f"• Time: <code>{now}</code>\n"
            f"• Total users who started: <b>{total}</b>"
        )
        await ctx.bot.send_message(group_id, T(text))
    except Exception as e:
        log.warning("log_user_start failed: %s", e)


def register(app):
    dual_command(app, "startlogs", startlogs_cmd)
    app.add_handler(CallbackQueryHandler(logs_callback, pattern=r"^logs:"))
