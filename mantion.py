"""Mention plugin:
@admin / @admins — tags all admins in the group (invisible mentions).
/admins / /adminlist — shows the list of admins.
"""
import html
import re

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes, MessageHandler, filters

from common import T, dual_command, say

ANON_ADMIN_ID = 1087968824

# Invisible character (Word Joiner) for silent mentions
INVISIBLE = "\u2060"


async def get_admin_info(ctx, chat_id: int):
    try:
        admins = await ctx.bot.get_chat_administrators(chat_id)
    except TelegramError:
        return None, None, "I need to be an admin to see the admin list!"

    admin_list = []
    admin_mentions = []  # list of (user_id, tag) for invisible tagging
    
    for admin in admins:
        if admin.user.id == ANON_ADMIN_ID or admin.user.is_bot:
            continue  # Skip anonymous admin dummy user and bots
            
        name = admin.user.first_name or "Admin"
        if admin.user.last_name:
            name += f" {admin.user.last_name}"
        
        # Visible tag for /adminlist
        if admin.user.username:
            tag = f"@{admin.user.username}"
        else:
            tag = f"<a href='tg://user?id={admin.user.id}'>{html.escape(name)}</a>"
        
        # Title logic
        if admin.status == ChatMemberStatus.OWNER:
            title = "Owner"
        elif admin.custom_title:
            title = html.escape(admin.custom_title)
        else:
            title = ""
        
        # Format for /adminlist
        if title:
            entry = f"- {tag} ({title})"
        else:
            entry = f"- {tag}"
            
        admin_list.append(entry)
        admin_mentions.append(admin.user.id)
        
    return admin_list, admin_mentions, None


async def admins_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return
        
    admin_list, _, error = await get_admin_info(ctx, chat.id)
    if error:
        await say(ctx, chat.id, T(error), reply_to=msg.message_id)
        return
        
    if not admin_list:
        await say(ctx, chat.id, T("No admins found."), reply_to=msg.message_id)
        return
        
    # Bold header with emoji
    header = f"👑 𝙰𝚍𝚖𝚒𝚗𝚜 𝚒𝚗 {html.escape(chat.title)}:"
    text = header + "\n" + "\n".join(admin_list)
    
    await say(ctx, chat.id, T(text), reply_to=msg.message_id)


async def admin_mention_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return
    if user and user.is_bot:
        return
        
    text = msg.text or msg.caption or ""
    if not re.search(r'(?i)@admins?\b', text):
        return
        
    _, admin_mentions, error = await get_admin_info(ctx, chat.id)
    if error or not admin_mentions:
        return
        
    # Telegram limits mentions to 5 per message to avoid spam flags
    chunk = admin_mentions[:5]
    
    # ✅ FIX: Create invisible mentions
    hidden_mentions = " ".join([f"<a href='tg://user?id={uid}'>{INVISIBLE}</a>" for uid in chunk])
    
    # Sirf yeh text dikhega, mentions chhupe rahenge
    reply_text = "𝙍𝙚𝙥𝙤𝙧𝙩𝙚𝙙 𝙖𝙙𝙢𝙞𝙣𝙨\n" + hidden_mentions
    
    await say(ctx, chat.id, T(reply_text), reply_to=msg.message_id)


def register(app):
    dual_command(app, "admins", admins_cmd)
    dual_command(app, "adminlist", admins_cmd)
    
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.TEXT & filters.Regex(r'(?i)@admins?\b'),
            admin_mention_handler
        ),
        group=5,
    )
