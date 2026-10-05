"""Purge module: .purge, .spurge, .del  (bulk deleteMessages based, Rose-style)."""
import asyncio
import logging

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

CHUNK = 100          # Telegram deleteMessages limit per call
MAX_RANGE = 5000     # safety cap on one purge
PASSES = 2           # 2nd pass catches anything missed in the 1st

_active_purges: set = set()   # chat_ids currently purging


async def auto_delete_msg(ctx, message, delay=10):
    await asyncio.sleep(delay)
    try:
        await ctx.bot.delete_message(message.chat_id, message.message_id)
    except Exception:
        pass


async def _warn(ctx, chat_id, text, delay=5):
    m = await ctx.bot.send_message(chat_id, f"<blockquote>{text}</blockquote>", parse_mode="HTML")
    asyncio.create_task(auto_delete_msg(ctx, m, delay))


async def check_purge_permissions(chat, user_id, ctx) -> bool:
    """User and bot both need 'can_delete_messages'."""
    try:
        member = await chat.get_member(user_id)
        if not isinstance(member, (ChatMemberOwner, ChatMemberAdministrator)):
            await _warn(ctx, chat.id, "You need to be an admin to use this command.")
            return False
        if isinstance(member, ChatMemberAdministrator) and not member.can_delete_messages:
            await _warn(ctx, chat.id, "You don't have the 'Delete Messages' right.")
            return False
    except Exception:
        return False

    try:
        bot_member = await chat.get_member(ctx.bot.id)
        if not isinstance(bot_member, (ChatMemberOwner, ChatMemberAdministrator)):
            await _warn(ctx, chat.id, "I need to be an admin to delete messages.")
            return False
        if isinstance(bot_member, ChatMemberAdministrator) and not bot_member.can_delete_messages:
            await _warn(ctx, chat.id, "I don't have the 'Delete Messages' right. Please promote me.")
            return False
    except Exception:
        return False

    return True


async def _delete_single(ctx, chat_id: int, msg_id: int):
    """Fallback: delete one id, retrying only on flood/transient errors."""
    for _ in range(4):
        try:
            await ctx.bot.delete_message(chat_id, msg_id)
            return
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except BadRequest:
            return            # not found / too old / undeletable -> nothing more to do
        except Forbidden:
            return
        except TelegramError as e:
            log.warning(f"delete {msg_id} transient error: {e}")
            await asyncio.sleep(0.5)
        except Exception as e:
            log.error(f"delete {msg_id} unexpected: {e}")
            return


async def _delete_chunk(ctx, chat_id: int, ids: list):
    """Bulk delete up to 100 ids. Missing/old ids are silently ignored by Telegram."""
    for _ in range(5):
        try:
            await ctx.bot.delete_messages(chat_id, ids)
            return
        except RetryAfter as e:
            log.warning(f"FloodWait {e.retry_after}s on bulk delete")
            await asyncio.sleep(e.retry_after + 1)
        except AttributeError:
            break             # old python-telegram-bot (<21): no delete_messages
        except Forbidden:
            return
        except BadRequest as e:
            log.warning(f"Bulk delete BadRequest: {e} -> falling back to single deletes")
            break
        except TelegramError as e:
            log.warning(f"Bulk delete error: {e}")
            await asyncio.sleep(0.5)
    # fallback path
    for mid in ids:
        await _delete_single(ctx, chat_id, mid)
        await asyncio.sleep(0.05)


async def _perfect_purge(ctx, chat_id: int, start_id: int, end_id: int):
    """Delete every id in [start_id, end_id] using bulk calls, in multiple passes."""
    if end_id - start_id + 1 > MAX_RANGE:
        start_id = end_id - MAX_RANGE + 1
    all_ids = list(range(start_id, end_id + 1))

    for p in range(PASSES):
        for i in range(0, len(all_ids), CHUNK):
            await _delete_chunk(ctx, chat_id, all_ids[i:i + CHUNK])
            await asyncio.sleep(0.05)
        if p < PASSES - 1:
            await asyncio.sleep(0.5)


async def _run_purge(update: Update, ctx: ContextTypes.DEFAULT_TYPE, silent: bool):
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

    if chat.id in _active_purges:
        return
    _active_purges.add(chat.id)
    try:
        start_id = msg.reply_to_message.message_id
        end_id = msg.message_id
        await _perfect_purge(ctx, chat.id, start_id, end_id)
    finally:
        _active_purges.discard(chat.id)

    if not silent:
        note = await ctx.bot.send_message(
            chat.id, "<blockquote>Purge Completed.</blockquote>", parse_mode="HTML"
        )
        asyncio.create_task(auto_delete_msg(ctx, note, 5))


async def purge_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _run_purge(update, ctx, silent=False)


async def spurge_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _run_purge(update, ctx, silent=True)


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

    await _delete_chunk(ctx, chat.id, [msg.reply_to_message.message_id, msg.message_id])


def register(app: Application):
    # concurrent handling so a long purge never blocks other updates
    app.add_handler(MessageHandler(
        filters.Regex(r"^[./]purge(?:@\w+)?$") & filters.ChatType.GROUPS, purge_cmd, block=False))
    app.add_handler(MessageHandler(
        filters.Regex(r"^[./]spurge(?:@\w+)?$") & filters.ChatType.GROUPS, spurge_cmd, block=False))
    app.add_handler(MessageHandler(
        filters.Regex(r"^[./](?:del|dly)(?:@\w+)?$") & filters.ChatType.GROUPS, del_cmd, block=False))
    
