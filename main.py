# -*- coding: utf-8 -*-
"""
main.py — single-file bot with the /promote admin-rights feature.

Secrets (BOT_TOKEN, MONGO_URI) are loaded from a local .env file — NEVER
hardcode them here, and NEVER commit .env to GitHub (keep it in .gitignore).

Setup:
    pip install -r requirements.txt
    cp .env.example .env      # then fill in your real token/uri in .env
    python3 main.py
"""

import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# --------------------------------------------------------------------------
# Config / setup
# --------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "telegram_bot")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing. Set it in your .env file (see .env.example).")

# --------------------------------------------------------------------------
# Permission catalogue for the /promote feature
# --------------------------------------------------------------------------
PERMISSIONS = [
    ("can_change_info", "Change Info"),
    ("can_delete_messages", "Delete Msgs"),
    ("can_invite_users", "Invite Users"),
    ("can_restrict_members", "Ban Users"),
    ("can_pin_messages", "Pin Msgs"),
    ("can_manage_video_chats", "Manage VC"),
    ("can_manage_chat", "Manage Chat"),
    ("is_anonymous", "Anonymous"),
    ("can_promote_members", "Add Admins"),
    ("can_manage_topics", "Manage Topics"),
]

PER_PAGE = 4
TOTAL_PAGES = (len(PERMISSIONS) + PER_PAGE - 1) // PER_PAGE  # 3 pages

# In-memory session store: {(chat_id, target_user_id): {perm_key: bool}}
_SESSIONS: dict[tuple[int, int], dict[str, bool]] = {}
_PAGES: dict[tuple[int, int], int] = {}

CB_TOGGLE = "promo:t"
CB_PAGE = "promo:p"
CB_CONFIRM = "promo:c"
CB_CANCEL = "promo:x"


def _session_key(chat_id: int, user_id: int) -> tuple[int, int]:
    return (chat_id, user_id)


def _build_keyboard(chat_id: int, user_id: int, page: int) -> InlineKeyboardMarkup:
    session = _SESSIONS[_session_key(chat_id, user_id)]
    start = page * PER_PAGE
    page_perms = PERMISSIONS[start:start + PER_PAGE]

    rows = []
    for i in range(0, len(page_perms), 2):
        row = []
        for key, label in page_perms[i:i + 2]:
            checked = "✅ " if session.get(key) else "▫️ "
            row.append(
                InlineKeyboardButton(
                    f"{checked}{label}",
                    callback_data=f"{CB_TOGGLE}:{chat_id}:{user_id}:{key}",
                )
            )
        rows.append(row)

    nav_row = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton("« Previous", callback_data=f"{CB_PAGE}:{chat_id}:{user_id}:{page - 1}")
        )
    if page < TOTAL_PAGES - 1:
        nav_row.append(
            InlineKeyboardButton("Next »", callback_data=f"{CB_PAGE}:{chat_id}:{user_id}:{page + 1}")
        )
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("✅ Confirm", callback_data=f"{CB_CONFIRM}:{chat_id}:{user_id}")])
    rows.append([InlineKeyboardButton("✖ Cancel", callback_data=f"{CB_CANCEL}:{chat_id}:{user_id}")])

    return InlineKeyboardMarkup(rows)


def _caption(target_name: str, page: int) -> str:
    return f"Select admin rights for <b>{target_name}</b>.\nPage {page + 1}/{TOTAL_PAGES}"


async def _actor_is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    member = await context.bot.get_chat_member(chat_id, update.effective_user.id)
    return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)


def _save_promotion(mongo_db, *, chat_id, actor_id, actor_name, target_id, target_name, rights):
    if mongo_db is None:
        logger.warning("No MongoDB database configured, skipping save.")
        return
    try:
        col = mongo_db["promotions"]
        col.insert_one(
            {
                "chat_id": chat_id,
                "actor_id": actor_id,
                "actor_name": actor_name,
                "target_id": target_id,
                "target_name": target_name,
                "rights": rights,
                "timestamp": datetime.now(timezone.utc),
            }
        )
    except Exception:
        logger.exception("Failed to save promotion record to MongoDB")


# --------------------------------------------------------------------------
# /promote command
# --------------------------------------------------------------------------
async def cmd_promote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return

    actor = update.effective_user
    actor_member = await chat.get_member(actor.id)
    if actor_member.status not in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
        await message.reply_text("Only group admins can promote members.")
        return

    if not message.reply_to_message:
        await message.reply_text("Reply to the user's message with /promote to start.")
        return

    target = message.reply_to_message.from_user
    if target.is_bot:
        await message.reply_text("Bots can't be promoted this way.")
        return

    key = _session_key(chat.id, target.id)
    _SESSIONS[key] = {p[0]: False for p in PERMISSIONS}
    _PAGES[key] = 0

    target_name = target.mention_html()
    await message.reply_text(
        _caption(target_name, 0),
        parse_mode=ParseMode.HTML,
        reply_markup=_build_keyboard(chat.id, target.id, 0),
    )


# --------------------------------------------------------------------------
# Callback handlers
# --------------------------------------------------------------------------
async def cb_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, _, chat_id, user_id, perm_key = query.data.split(":")
    chat_id, user_id = int(chat_id), int(user_id)
    key = _session_key(chat_id, user_id)

    if key not in _SESSIONS:
        await query.answer("This promotion session expired. Run /promote again.", show_alert=True)
        return
    if not await _actor_is_admin(update, context, chat_id):
        await query.answer("Only group admins can do this.", show_alert=True)
        return

    _SESSIONS[key][perm_key] = not _SESSIONS[key][perm_key]
    page = _PAGES.get(key, 0)
    await query.edit_message_reply_markup(reply_markup=_build_keyboard(chat_id, user_id, page))
    await query.answer()


async def cb_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, _, chat_id, user_id, page = query.data.split(":")
    chat_id, user_id, page = int(chat_id), int(user_id), int(page)
    key = _session_key(chat_id, user_id)

    if key not in _SESSIONS:
        await query.answer("This promotion session expired. Run /promote again.", show_alert=True)
        return
    if not await _actor_is_admin(update, context, chat_id):
        await query.answer("Only group admins can do this.", show_alert=True)
        return

    _PAGES[key] = page
    target_name = (await context.bot.get_chat_member(chat_id, user_id)).user.mention_html()
    await query.edit_message_text(
        _caption(target_name, page),
        parse_mode=ParseMode.HTML,
        reply_markup=_build_keyboard(chat_id, user_id, page),
    )
    await query.answer()


async def cb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, _, chat_id, user_id = query.data.split(":")
    chat_id, user_id = int(chat_id), int(user_id)
    key = _session_key(chat_id, user_id)

    if not await _actor_is_admin(update, context, chat_id):
        await query.answer("Only group admins can do this.", show_alert=True)
        return

    _SESSIONS.pop(key, None)
    _PAGES.pop(key, None)
    await query.edit_message_text("Promotion cancelled.")
    await query.answer()


async def cb_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, _, chat_id, user_id = query.data.split(":")
    chat_id, user_id = int(chat_id), int(user_id)
    key = _session_key(chat_id, user_id)

    if key not in _SESSIONS:
        await query.answer("This promotion session expired. Run /promote again.", show_alert=True)
        return
    if not await _actor_is_admin(update, context, chat_id):
        await query.answer("Only group admins can do this.", show_alert=True)
        return

    rights = _SESSIONS[key]
    actor = update.effective_user

    try:
        await context.bot.promote_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            can_change_info=rights["can_change_info"],
            can_delete_messages=rights["can_delete_messages"],
            can_invite_users=rights["can_invite_users"],
            can_restrict_members=rights["can_restrict_members"],
            can_pin_messages=rights["can_pin_messages"],
            can_manage_video_chats=rights["can_manage_video_chats"],
            can_manage_chat=rights["can_manage_chat"],
            is_anonymous=rights["is_anonymous"],
            can_promote_members=rights["can_promote_members"],
            can_manage_topics=rights["can_manage_topics"],
        )
    except (BadRequest, Forbidden) as e:
        await query.answer()
        await query.edit_message_text(f"Failed to promote: {e.message if hasattr(e, 'message') else e}")
        _SESSIONS.pop(key, None)
        _PAGES.pop(key, None)
        return

    target_user = (await context.bot.get_chat_member(chat_id, user_id)).user
    granted = [label for pkey, label in PERMISSIONS if rights[pkey]]

    _save_promotion(
        context.bot_data.get("mongo_db"),
        chat_id=chat_id,
        actor_id=actor.id,
        actor_name=actor.full_name,
        target_id=user_id,
        target_name=target_user.full_name,
        rights=rights,
    )

    _SESSIONS.pop(key, None)
    _PAGES.pop(key, None)

    granted_text = ", ".join(granted) if granted else "no special rights (admin badge only)"
    await query.edit_message_text(
        f"✅ {target_user.mention_html()} promoted with: {granted_text}",
        parse_mode=ParseMode.HTML,
    )
    await query.answer("Promoted!")


# --------------------------------------------------------------------------
# Misc commands + entry point
# --------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Bot is up. Reply to a member's message with /promote in a group to grant admin rights."
    )


def main() -> None:
    mongo_client = MongoClient(MONGO_URI)
    mongo_db = mongo_client[MONGO_DB_NAME]

    try:
        mongo_client.admin.command("ping")
        logger.info("Connected to MongoDB (%s)", MONGO_DB_NAME)
    except Exception:
        logger.exception("Could not connect to MongoDB — check MONGO_URI in .env")
        raise

    application = Application.builder().token(BOT_TOKEN).build()
    application.bot_data["mongo_db"] = mongo_db

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("promote", cmd_promote))
    application.add_handler(CallbackQueryHandler(cb_toggle, pattern=rf"^{CB_TOGGLE}:"))
    application.add_handler(CallbackQueryHandler(cb_page, pattern=rf"^{CB_PAGE}:"))
    application.add_handler(CallbackQueryHandler(cb_confirm, pattern=rf"^{CB_CONFIRM}:"))
    application.add_handler(CallbackQueryHandler(cb_cancel, pattern=rf"^{CB_CANCEL}:"))

    logger.info("Bot starting (polling)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
              
