"""Purge module: .purge, .spurge, .del (Rose-style bulk delete logic)."""
import asyncio
import logging
from collections import Counter

from telegram import ChatMemberAdministrator, ChatMemberOwner, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from common import log

COMMANDS = [
    ("purge", "Purge messages from replied message to this command"),
    ("spurge", "Silent purge (no confirmation note)"),
    ("del", "Delete the replied message"),
]

CHUNK = 100            # Telegram deleteMessages limit per call
MAX_RANGE = 5000       # safety cap on one purge

_active_purges: set = set()   # chat_ids currently purging


# ✅ NAYA: Original bot wala divide_chunks function
def divide_chunks(l: list, n: int = 100):
    """Split a list into chunks of maximum size n."""
    for i in range(0, len(l), n):
        yield l[i:i + n]


async def auto_delete_msg(ctx, message, delay=10):
    await asyncio.sleep(delay)
    try:
        await ctx.bot.delete_message(message.chat_id, message.message_id)
    except Exception:
        pass


async def _warn(ctx, chat_id, text, delay=5):
    m = await ctx.bot.send_message(chat_id, f"<blockquote>{text}</blockquote>", parse_mode="HTML")
    asyncio.create_task(auto_delete_msg(ctx, m, delay))


def _is_anon_admin(update: Update) -> bool:
    """Admin posting as the group itself (anonymous admin)."""
    m = update.effective_message
    return bool(m and m.sender_chat and m.sender_chat.id == update.effective_chat.id)


async def check_purge_permissions(update: Update, ctx) -> bool:
    chat = update.effective_chat
    user = update.effective_user

    if not _is_anon_admin(update):
        try:
            member = await chat.get_member(user.id)
        except Exception as e:
            log.warning(f"purge: get_member(user) failed: {e}")
            return False
        if not isinstance(member, (ChatMemberOwner, ChatMemberAdministrator)):
            await _warn(ctx, chat.id, "You need to be an admin to use this command.")
            return False
        if isinstance(member, ChatMemberAdministrator) and not member.can_delete_messages:
            await _warn(ctx, chat.id, "You don't have the 'Delete Messages' right.")
            return False

    try:
        bot_member = await chat.get_member(ctx.bot.id)
    except Exception as e:
        log.warning(f"purge: get_member(bot) failed: {e}")
        return False
    if not isinstance(bot_member, (ChatMemberOwner, ChatMemberAdministrator)):
        await _warn(ctx, chat.id, "I need to be an admin to delete messages.")
        return False
    if isinstance(bot_member, ChatMemberAdministrator) and not bot_member.can_delete_messages:
        await _warn(ctx, chat.id, "I don't have the 'Delete Messages' right. Please promote me.")
        return False
    return True


async def _bulk_delete(ctx, chat_id: int, ids: list):
    """Bulk delete messages in chunks of 100 (Original bot logic)."""
    if not ids:
        return
    
    # ✅ Original bot wala logic: divide into chunks
    chunks = list(divide_chunks(ids, CHUNK))
    
    for chunk in chunks:
        for _ in range(5): # Retry loop
            try:
                await ctx.bot.delete_messages(chat_id, chunk)
                break # Success, break retry loop
            except RetryAfter as e:
                log.warning(f"FloodWait {e.retry_after}s on bulk delete")
                await asyncio.sleep(e.retry_after + 1)
            except AttributeError:
                # PTB < 21 fallback: delete one by one
                for mid in chunk:
                    try:
                        await ctx.bot.delete_message(chat_id, mid)
                    except Exception:
                        pass
                break
            except Forbidden as e:
                log.warning(f"Bulk delete Forbidden: {e}")
                return
            except BadRequest as e:
                log.warning(f"Bulk delete BadRequest: {e} -> trying single fallback")
                # Fallback to single delete if bulk fails
                for mid in chunk:
                    try:
                        await ctx.bot.delete_message(chat_id, mid)
                    except Exception:
                        pass
                break
            except TelegramError as e:
                log.warning(f"Bulk delete error: {e}")
                await asyncio.sleep(0.5)
        await asyncio.sleep(0.05) # Small delay between chunks


async def _run_purge(update: Update, ctx: ContextTypes.DEFAULT_TYPE, silent: bool):
    msg = update.effective_message
    chat = update.effective_chat

    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("<blockquote>This command works only in groups.</blockquote>", parse_mode="HTML")
        return

    if not msg.reply_to_message:
        m = await msg.reply_text("<blockquote>Reply to a message to start purging.</blockquote>", parse_mode="HTML")
        asyncio.create_task(auto_delete_msg(ctx, m, 5))
        return

    if not await check_purge_permissions(update, ctx):
        return

    if chat.id in _active_purges:
        return
    _active_purges.add(chat.id)
    
    try:
        start_id = msg.reply_to_message.message_id
        end_id = msg.message_id
        
        # Safety cap
        if end_id - start_id + 1 > MAX_RANGE:
            start_id = end_id - MAX_RANGE + 1
            
        # Message IDs ki list banao (reply wale se command wale tak)
        message_ids = list(range(start_id, end_id + 1))
        
        # ✅ Original bot logic: Bulk delete in chunks
        await _bulk_delete(ctx, chat.id, message_ids)
        
    finally:
        _active_purges.discard(chat.id)

    if not silent:
        text = "Purge Completed."
        note = await ctx.bot.send_message(chat.id, f"<blockquote>{text}</blockquote>", parse_mode="HTML")
        asyncio.create_task(auto_delete_msg(ctx, note, 5))


async def purge_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _run_purge(update, ctx, silent=False)


async def spurge_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _run_purge(update, ctx, silent=True)


async def del_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat

    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("<blockquote>This command works only in groups.</blockquote>", parse_mode="HTML")
        return

    if not msg.reply_to_message:
        m = await msg.reply_text("<blockquote>Reply to a message to delete it.</blockquote>", parse_mode="HTML")
        asyncio.create_task(auto_delete_msg(ctx, m, 5))
        return

    if not await check_purge_permissions(update, ctx):
        return

    # Delete command message and replied message
    await _bulk_delete(ctx, chat.id, [msg.message_id, msg.reply_to_message.message_id])


def register(app: Application):
    app.add_handler(MessageHandler(
        filters.Regex(r"^[./]purge(?:@\w+)?$") & filters.ChatType.GROUPS, purge_cmd, block=False))
    app.add_handler(MessageHandler(
        filters.Regex(r"^[./]spurge(?:@\w+)?$") & filters.ChatType.GROUPS, spurge_cmd, block=False))
    app.add_handler(MessageHandler(
        filters.Regex(r"^[./](?:del|dly)(?:@\w+)?$") & filters.ChatType.GROUPS, del_cmd, block=False))
