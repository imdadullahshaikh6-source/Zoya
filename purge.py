"""Purge module: provides .purge, .spurge, and .del commands with optimized bulk deletion."""
import asyncio
import logging

from telegram import ChatMemberAdministrator, ChatMemberOwner, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import Application, MessageHandler, filters, ContextTypes

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
            m = await ctx.bot.send_message(chat.id, "<blockquote>You need to be an admin to use this command.</blockquote>", parse_mode="HTML")
            asyncio.create_task(auto_delete_msg(ctx, m, 5))
            return False
        if isinstance(member, ChatMemberAdministrator) and not member.can_delete_messages:
            m = await ctx.bot.send_message(chat.id, "<blockquote>You don't have the 'Delete Messages' right.</blockquote>", parse_mode="HTML")
            asyncio.create_task(auto_delete_msg(ctx, m, 5))
            return False
    except Exception:
        return False

    # Check bot
    try:
        bot_member = await chat.get_member(ctx.bot.id)
        if not isinstance(bot_member, (ChatMemberOwner, ChatMemberAdministrator)):
            m = await ctx.bot.send_message(chat.id, "<blockquote>I need to be an admin to delete messages.</blockquote>", parse_mode="HTML")
            asyncio.create_task(auto_delete_msg(ctx, m, 5))
            return False
        if isinstance(bot_member, ChatMemberAdministrator) and not bot_member.can_delete_messages:
            m = await ctx.bot.send_message(chat.id, "<blockquote>I don't have the 'Delete Messages' right. Please promote me.</blockquote>", parse_mode="HTML")
            asyncio.create_task(auto_delete_msg(ctx, m, 5))
            return False
    except Exception:
        return False

    return True


async def _fast_purge(ctx, chat_id: int, start_id: int, end_id: int) -> int:
    """Optimized purge logic: bulk delete in chunks of 100 with fallback."""
    message_ids = list(range(start_id, end_id + 1))
    deleted_count = 0
    
    # Telegram API allows up to 100 messages to be deleted in a single request.
    # We process in chunks of 100 for maximum speed.
    for i in range(0, len(message_ids), 100):
        batch = message_ids[i:i+100]
        try:
            # Bulk delete (very fast)
            await ctx.bot.delete_messages(chat_id, batch)
            deleted_count += len(batch)
            # Small delay between batches to avoid hitting FloodWait limits
            await asyncio.sleep(0.2)
        except TelegramError as e:
            # If bulk delete fails (e.g., one message is already deleted or >48 hours old),
            # fall back to deleting them one by one for this specific batch.
            for msg_id in batch:
                try:
                    await ctx.bot.delete_message(chat_id, msg_id)
                    deleted_count += 1
                    await asyncio.sleep(0.05)  # Minimal delay for individual fallback
                except TelegramError:
                    continue
    return deleted_count


async def purge_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("<blockquote>This command works only in groups.</blockquote>", parse_mode="HTML")
        return

    if not msg.reply_to_message:
        m = await msg.reply_text("<blockquote>Reply to a message to start purging.</blockquote>", parse_mode="HTML")
        asyncio.create_task(auto_delete_msg(ctx, m, 5))
        return

    if not await check_purge_permissions(chat, user.id, ctx):
        return

    start_id = msg.reply_to_message.message_id
    end_id = msg.message_id
    chat_id = chat.id

    # ✅ UPDATED: Fast bulk deletion
    deleted_count = await _fast_purge(ctx, chat_id, start_id, end_id)

    note = await ctx.bot.send_message(chat_id, f"<blockquote>Successfully purged {deleted_count} messages.</blockquote>", parse_mode="HTML")
    asyncio.create_task(auto_delete_msg(ctx, note, 10))


async def spurge_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("<blockquote>This command works only in groups.</blockquote>", parse_mode="HTML")
        return

    if not msg.reply_to_message:
        m = await msg.reply_text("<blockquote>Reply to a message to start purging.</blockquote>", parse_mode="HTML")
        asyncio.create_task(auto_delete_msg(ctx, m, 5))
        return

    if not await check_purge_permissions(chat, user.id, ctx):
        return

    start_id = msg.reply_to_message.message_id
    end_id = msg.message_id
    chat_id = chat.id

    # ✅ UPDATED: Fast silent bulk deletion
    await _fast_purge(ctx, chat_id, start_id, end_id)


async def del_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("<blockquote>This command works only in groups.</blockquote>", parse_mode="HTML")
        return

    if not msg.reply_to_message:
        m = await msg.reply_text("<blockquote>Reply to a message to delete it.</blockquote>", parse_mode="HTML")
        asyncio.create_task(auto_delete_msg(ctx, m, 5))
        return

    if not await check_purge_permissions(chat, user.id, ctx):
        return

    try:
        # Delete replied message and the command message
        await ctx.bot.delete_message(chat.id, msg.reply_to_message.message_id)
        await ctx.bot.delete_message(chat.id, msg.message_id)
    except Exception as e:
        log.error(f"Del error: {e}")


def register(app: Application):
    # ✅ Regex supports both / and . prefixes for all purge commands
    app.add_handler(MessageHandler(
        filters.Regex(r"^[./]purge(?:@\w+)?$") & filters.ChatType.GROUPS, 
        purge_cmd
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"^[./]spurge(?:@\w+)?$") & filters.ChatType.GROUPS, 
        spurge_cmd
    ))
    # .del aur .dly dono ke liye same handler
    app.add_handler(MessageHandler(
        filters.Regex(r"^[./](?:del|dly)(?:@\w+)?$") & filters.ChatType.GROUPS, 
        del_cmd
    ))
