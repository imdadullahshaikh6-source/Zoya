"""Purge module: provides .purge, .spurge, and .del commands with perfect deletion."""
import asyncio
import logging

from telegram import ChatMemberAdministrator, ChatMemberOwner, Update
from telegram.constants import ChatType
from telegram.error import TelegramError, RetryAfter
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


async def _perfect_purge(ctx, chat_id: int, start_id: int, end_id: int):
    """One-by-one deletion with smart delay. Handles invalid messages gracefully."""
    for msg_id in range(start_id, end_id + 1):
        try:
            await ctx.bot.delete_message(chat_id, msg_id)
            # ✅ Success! Fast delay for speed (0.05s = 20 msgs/sec)
            await asyncio.sleep(0.05)
        except RetryAfter as e:
            # Telegram rate limit, wait it out
            log.warning(f"FloodWait hit! Sleeping for {e.retry_after} seconds.")
            await asyncio.sleep(e.retry_after + 1)
            try:
                await ctx.bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
        except TelegramError as e:
            err_str = str(e).lower()
            # ✅ FIX: If message doesn't exist or can't be deleted (400 Bad Request), 
            # skip it immediately without wasting time.
            if "message to delete not found" in err_str or "message can't be deleted" in err_str or "bad request" in err_str:
                await asyncio.sleep(0.01)  # Very tiny pause to avoid API hammering
                continue
            else:
                log.warning(f"Failed to delete {msg_id}: {e}")
                await asyncio.sleep(0.1)
                continue
        except Exception as e:
            log.error(f"Unexpected error deleting {msg_id}: {e}")
            continue


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

    # ✅ Perfect Purge
    await _perfect_purge(ctx, chat_id, start_id, end_id)

    # ✅ Purge Completed message
    note = await ctx.bot.send_message(chat_id, "<blockquote>Purge Completed.</blockquote>", parse_mode="HTML")
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

    # ✅ Perfect Silent Purge
    await _perfect_purge(ctx, chat_id, start_id, end_id)


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
