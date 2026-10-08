"""Purge module: .purge, .spurge, .del  (Rose-style: newest -> oldest bulk delete + verify pass)."""
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
VERIFY_LIMIT = 1500    # single-delete verify pass only up to this many ids
VERIFY_CONCURRENCY = 15

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


async def _delete_one(ctx, chat_id: int, mid: int, stats: Counter, reasons: Counter):
    """Single delete that REPORTS why it failed (instead of silently ignoring)."""
    for _ in range(4):
        try:
            await ctx.bot.delete_message(chat_id, mid)
            stats["deleted_in_verify"] += 1
            return
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except BadRequest as e:
            txt = str(e).lower()
            if "not found" in txt:          # already gone / never existed -> fine
                stats["gone"] += 1
            else:                           # e.g. "message can't be deleted"
                stats["failed"] += 1
                reasons[str(e)] += 1
            return
        except Forbidden as e:
            stats["failed"] += 1
            reasons[f"Forbidden: {e}"] += 1
            return
        except TelegramError:
            await asyncio.sleep(0.5)
    stats["failed"] += 1
    reasons["retries exhausted"] += 1


async def _delete_chunk(ctx, chat_id: int, ids: list):
    """Bulk delete up to 100 ids. Missing/old ids are skipped by Telegram."""
    for _ in range(5):
        try:
            await ctx.bot.delete_messages(chat_id, ids)
            return
        except RetryAfter as e:
            log.warning(f"FloodWait {e.retry_after}s on bulk delete")
            await asyncio.sleep(e.retry_after + 1)
        except AttributeError:
            break   # PTB < 21: no delete_messages
        except Forbidden as e:
            log.warning(f"Bulk delete Forbidden: {e}")
            return
        except BadRequest as e:
            log.warning(f"Bulk delete BadRequest: {e} -> single fallback")
            break
        except TelegramError as e:
            log.warning(f"Bulk delete error: {e}")
            await asyncio.sleep(0.5)
    stats, reasons = Counter(), Counter()
    for mid in ids:
        await _delete_one(ctx, chat_id, mid, stats, reasons)
        await asyncio.sleep(0.03)


async def _perfect_purge(ctx, chat_id: int, start_id: int, end_id: int) -> Counter:
    """Delete [start_id, end_id] newest -> oldest (like Rose), then verify leftovers."""
    if end_id - start_id + 1 > MAX_RANGE:
        start_id = end_id - MAX_RANGE + 1
    ids = list(range(end_id, start_id - 1, -1))      # newest first

    # pass 1 + 2: bulk
    for _ in range(2):
        for i in range(0, len(ids), CHUNK):
            await _delete_chunk(ctx, chat_id, ids[i:i + CHUNK])
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)

    # verify pass: anything still deletable gets deleted; real failures are logged with Telegram's reason
    stats, reasons = Counter(), Counter()
    if len(ids) <= VERIFY_LIMIT:
        sem = asyncio.Semaphore(VERIFY_CONCURRENCY)

        async def run(mid):
            async with sem:
                await _delete_one(ctx, chat_id, mid, stats, reasons)

        await asyncio.gather(*(run(m) for m in ids))
        if stats["failed"]:
            log.warning(f"purge chat={chat_id}: {stats['failed']} undeletable. reasons={dict(reasons)}")
        if stats["deleted_in_verify"]:
            log.info(f"purge chat={chat_id}: verify pass caught {stats['deleted_in_verify']} missed msgs")
    return stats


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
    stats = Counter()
    try:
        start_id = msg.reply_to_message.message_id
        end_id = msg.message_id
        stats = await _perfect_purge(ctx, chat.id, start_id, end_id)
    finally:
        _active_purges.discard(chat.id)

    if not silent:
        text = "Purge Completed."
        if stats.get("failed"):
            text += f"\n{stats['failed']} message(s) could not be deleted by Telegram."
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

    await _delete_chunk(ctx, chat.id, [msg.message_id, msg.reply_to_message.message_id])


def register(app: Application):
    app.add_handler(MessageHandler(
        filters.Regex(r"^[./]purge(?:@\w+)?$") & filters.ChatType.GROUPS, purge_cmd, block=False))
    app.add_handler(MessageHandler(
        filters.Regex(r"^[./]spurge(?:@\w+)?$") & filters.ChatType.GROUPS, spurge_cmd, block=False))
    app.add_handler(MessageHandler(
        filters.Regex(r"^[./](?:del|dly)(?:@\w+)?$") & filters.ChatType.GROUPS, del_cmd, block=False))
    
