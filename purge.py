"""Purge module: provides /purge, /spurge, and /del commands."""
import asyncio
import logging

from telegram import ChatMemberAdministrator, ChatMemberOwner, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from common import log

COMMANDS = [
    ("purge", "Purge messages from replied message to this command"),
    ("spurge", "Silent purge (no confirmation note)"),
    ("del", "Delete the replied message"),
]


async def auto_delete_msg(ctx, message, delay=10):
    """Helper to auto-delete a message after a specified delay."""
    await asyncio.sleep(delay)
    try:
        await ctx.bot.delete_message(message.chat_id, message.message_id)
    except Exception:
        pass


async def check_purge_permissions(chat, user_id, ctx) -> bool:
    """Checks if both the user and bot have 'can_delete_messages' rights."""
    # Check user
    try:
        member = await chat.get_member(user_id)
        if not isinstance(member, (ChatMemberOwner, ChatMemberAdministrator)):
            m = await ctx.bot.send_message(chat.id, "You need to be an admin to use this command.")
            asyncio.create_task(auto_delete_msg(ctx, m, 5))
            return False
        if isinstance(member, ChatMemberAdministrator) and not member.can_delete_messages:
            m = await ctx.bot.send_message(chat.id, "You don't have the 'Delete Messages' right.")
            asyncio.create_task(auto_delete_msg(ctx, m, 5))
            return False
    except Exception:
        return False

    # Check bot
    try:
        bot_member = await chat.get_member(ctx.bot.id)
        if not isinstance(bot_member, (ChatMemberOwner, ChatMemberAdministrator)):
            m = await ctx.bot.send_message(chat.id, "I need to be an admin to delete messages.")
            asyncio.create_task(auto_delete_msg(ctx, m, 5))
            return False
        if isinstance(bot_member, ChatMemberAdministrator) and not bot_member.can_delete_messages:
            m = await ctx.bot.send_message(chat.id, "I don't have the 'Delete Messages' right. Please promote me.")
            asyncio.create_task(auto_delete_msg(ctx, m, 5))
            return False
    except Exception:
        return False

    return True


async def purge_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("This command works only in groups.")
        return

    if not msg.reply_to_message:
        m = await msg.reply_text("Reply to a message to start purging.")
        asyncio.create_task(auto_delete_msg(ctx, m, 5))
        return

    if not await check_purge_permissions(chat, user.id, ctx):
        return

    start_id = msg.reply_to_message.message_id
    end_id = msg.message_id
    chat_id = chat.id

    message_ids = list(range(start_id, end_id + 1))
    for i in range(0, len(message_ids), 100):
        batch = message_ids[i:i+100]
        try:
            await ctx.bot.delete_messages(chat_id, batch)
        except Exception as e:
            log.error(f"Purge error: {e}")

    note = await ctx.bot.send_message(chat_id, f"Successfully purged {len(message_ids)} messages.")
    asyncio.create_task(auto_delete_msg(ctx, note, 10))


async def spurge_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("This command works only in groups.")
        return

    if not msg.reply_to_message:
        m = await msg.reply_text("Reply to a message to start purging.")
        asyncio.create_task(auto_delete_msg(ctx, m, 5))
        return

    if not await check_purge_permissions(chat, user.id, ctx):
        return

    start_id = msg.reply_to_message.message_id
    end_id = msg.message_id
    chat_id = chat.id

    message_ids = list(range(start_id, end_id + 1))
    for i in range(0, len(message_ids), 100):
        batch = message_ids[i:i+100]
        try:
            await ctx.bot.delete_messages(chat_id, batch)
        except Exception as e:
            log.error(f"SPurge error: {e}")


async def del_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("This command works only in groups.")
        return

    if not msg.reply_to_message:
        m = await msg.reply_text("Reply to a message to delete it.")
        asyncio.create_task(auto_delete_msg(ctx, m, 5))
        return

    if not await check_purge_permissions(chat, user.id, ctx):
        return

    try:
        await ctx.bot.delete_messages(chat.id, [msg.reply_to_message.message_id, msg.message_id])
    except Exception as e:
        log.error(f"Del error: {e}")


def register(app: Application):
    app.add_handler(CommandHandler("purge", purge_cmd))
    app.add_handler(CommandHandler("spurge", spurge_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
