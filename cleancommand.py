"""Auto-delete commands in groups.

Commands:
  .cleancommand [all|admin|users]  — enable cleaning
  .keepcommand                     — disable cleaning

Categories:
  all    → delete every command after it's used
  admin  → only moderation/config commands (ban, mute, kick, promote, ...)
  users  → only user-facing commands (q, kang, waifu, ...)

The bot's own messages and its own replies are never touched.
Data is stored in MongoDB per chat_id."""
import logging

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes, MessageHandler, filters

import database as dbase
from common import T, dual_command, say

log = logging.getLogger("cleancommand")

HELP_TXT = (
    "<b>🧹 𝘾𝙡𝙚𝙖𝙣 𝘾𝙤𝙢𝙢𝙖𝙣𝙙</b>\n\n"
    "Automatically delete commands from the chat after they're used, "
    "so the group stays clean.\n\n"
    "<b>Enable / Disable:</b>\n"
    "• <code>.cleancommand all</code> — delete every command after use.\n"
    "• <code>.cleancommand admin</code> — delete only admin/mod commands "
    "(ban, mute, kick, promote, ...).\n"
    "• <code>.cleancommand users</code> — delete only user commands "
    "(q, kang, waifu, ...).\n"
    "• <code>.keepcommand</code> — stop auto-deleting commands.\n\n"
    "<i>Only full admins can change this. Settings are per-chat and survive restarts.</i>"
)
COMMANDS = [
    ("cleancommand", "Auto-delete commands in this chat"),
    ("keepcommand", "Stop auto-deleting commands"),
]

ADMIN_COMMANDS = {
    "ban", "unban", "kick", "mute", "unmute", "warn", "unwarn",
    "resetwarn", "warns",
    "promote", "demote", "settitle",
    "welcome", "setwelcome", "resetwelcome", "setwelcomepic",
    "filter", "filters", "setfilter", "rmfilter", "clearfilters",
    "filterlist", "stop",
    "setdelay", "guard", "permit", "unpermit", "permitlist",
    "afk",
    "pin", "unpin", "unpinall",
}

SELF_COMMANDS = {"cleancommand", "keepcommand"}


async def _is_full_admin(ctx, chat_id: int, user_id: int) -> bool:
    try:
        m = await ctx.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    if m.status == ChatMemberStatus.OWNER:
        return True
    if m.status != ChatMemberStatus.ADMINISTRATOR:
        return False
    return bool(getattr(m, "can_delete_messages", False))


# ───────────── commands ─────────────

async def clean_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return
    if not await _is_full_admin(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("⚠️ only full-power admins can use this."), reply_to=msg.message_id)
        return
    mode = (ctx.args[0].lower() if ctx.args else "all")
    if mode not in ("all", "admin", "users"):
        await say(ctx, chat.id, T("usage: <code>.cleancommand all</code> | <code>admin</code> | <code>users</code>"), reply_to=msg.message_id)
        return
    await dbase.clean_set(chat.id, enabled=True, mode=mode)
    await say(ctx, chat.id, T(f"🧹 Clean command enabled — mode: <b>{mode}</b>."), reply_to=msg.message_id)


async def keep_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return
    if not await _is_full_admin(ctx, chat.id, user.id):
        await say(ctx, chat.id, T("⚠️ only full-power admins can use this."), reply_to=msg.message_id)
        return
    await dbase.clean_set(chat.id, enabled=False, mode="off")
    await say(ctx, chat.id, T("🧹 Clean command disabled — commands will stay in chat."), reply_to=msg.message_id)


# ───────────── watcher ─────────────

async def _clean_watcher(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Runs at group=-2 so it fires before any command handler.
    Deletes the user's command message if cleaning is on for this chat.
    block=False so downstream command handlers still run."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if user.is_bot:
        return

    text = (msg.text or msg.caption or "").lstrip()
    if not text or text[0] not in ("/", "."):
        return

    first = text[1:].split()[0].split("@")[0].lower()
    if not first:
        return
    if first in SELF_COMMANDS:
        log.info("[clean] skip self-command: %s", first)
        return

    cfg = await dbase.clean_get(chat.id)
    log.info("[clean] caught '%s' in chat %s — cfg=%s", first, chat.id, cfg)
    if not cfg or not cfg.get("enabled"):
        return

    mode = cfg.get("mode", "all")
    if mode == "all":
        should = True
    elif mode == "admin":
        should = first in ADMIN_COMMANDS
    elif mode == "users":
        should = first not in ADMIN_COMMANDS
    else:
        should = False

    if not should:
        log.info("[clean] '%s' not in mode '%s' — skipping", first, mode)
        return

    try:
        await ctx.bot.delete_message(chat.id, msg.message_id)
        log.info("[clean] ✅ DELETED '%s' from chat %s", first, chat.id)
    except TelegramError as e:
        log.warning("[clean] ❌ delete failed for '%s': %s", first, e)


# ───────────── registration ─────────────

def register(app):
    dual_command(app, "cleancommand", clean_cmd)
    dual_command(app, "keepcommand", keep_cmd)

    # group=-2 → runs before every other handler.
    # block=False is passed to MessageHandler (NOT add_handler) so command
    # handlers in higher groups still run — we only delete the user's
    # command message, we don't consume the update.
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & filters.ChatType.GROUPS,
            _clean_watcher,
            block=False,
        ),
        group=-2,
    )
