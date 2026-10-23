"""Hidden owner-only broadcast (groups only, not in help/menu): reply to any message with /broadcast (or .broadcast) and it is
forwarded to every active user and chat. Dead targets (blocked / kicked) are marked
inactive in the DB so later broadcasts skip them. Ported from the Pyrogram version."""
import asyncio

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, ChatMigrated, Forbidden, RetryAfter, TelegramError
from telegram.ext import ContextTypes

import database as dbase
from common import T, dual_command, esc, log, mention, q, say

BROADCAST_OWNER_ID = 8373739674   # only this user can broadcast
PAUSE = 0.05                      # sleep after every send (flood safety)
EDIT_EVERY = 50                   # refresh the status message every N targets
COMMANDS = []  # owner-only + hidden → not in "/" menu, not in help pages

# BadRequest texts that mean "this target is gone" → safe to mark dead
_DEAD_HINTS = (
    "chat not found", "user not found", "peer_id_invalid", "user_is_deactivated",
    "chat_write_forbidden", "have no rights to send", "not enough rights to send",
    "group chat was deactivated", "chat_restricted",
)
# BadRequest texts that mean "the SOURCE message is the problem" → stop, never mark anyone dead
_SOURCE_HINTS = ("message to forward not found", "can't be forwarded", "message_id_invalid")


class _Abort(Exception):
    """Raised when the replied message itself can't be forwarded (so every send would fail)."""


def _secs(v) -> float:
    """RetryAfter.retry_after is an int (or a timedelta on newer PTB) → seconds."""
    return v.total_seconds() if hasattr(v, "total_seconds") else float(v)


async def _forward(ctx, target: int, col: str, from_chat: int, msg_id: int) -> str:
    """Forward once (retry once on flood-wait). Returns 'ok' | 'fail' | 'dead'."""
    for attempt in (1, 2):
        try:
            await ctx.bot.forward_message(target, from_chat, msg_id)
            return "ok"
        except RetryAfter as e:
            if attempt == 2:
                return "fail"
            await asyncio.sleep(_secs(e.retry_after) + 1)  # wait as told, then retry
        except BadRequest as e:
            s = str(e).lower()
            if any(h in s for h in _SOURCE_HINTS):
                raise _Abort(str(e))
            if any(h in s for h in _DEAD_HINTS):
                await dbase.mark_broadcast_dead(col, target)
                return "dead"
            return "fail"  # unknown BadRequest: count it, but don't deactivate anyone
        except (Forbidden, ChatMigrated):
            # blocked the bot / deactivated / bot kicked / group upgraded to supergroup
            await dbase.mark_broadcast_dead(col, target)
            return "dead"
        except TelegramError as e:  # network, timeout, etc.
            log.warning("[broadcast] %s failed: %s", target, e)
            return "fail"
    return "fail"


async def _edit(status, text: str):
    try:
        await status.edit_text(q(text), parse_mode=ParseMode.HTML)
    except TelegramError:
        pass  # "not modified" / flood on edit — progress is cosmetic, ignore


async def _run(ctx, status, owner, from_chat: int, msg_id: int, targets: list):
    """Background worker: loops over targets, reports progress, shows final stats."""
    total, done, sent, failed, dead = len(targets), 0, 0, 0, 0
    reason = None
    try:
        for col, tid in targets:
            res = await _forward(ctx, tid, col, from_chat, msg_id)
            done += 1
            if res == "ok":
                sent += 1
            else:
                failed += 1
                dead += res == "dead"
            await asyncio.sleep(PAUSE)
            if done % EDIT_EVERY == 0:
                await _edit(status, T(
                    "<b>broadcasting...</b>\n\nprocessed: {p}/{t} | sent: {s} | failed: {f}",
                    p=done, t=total, s=sent, f=failed,
                ))
    except _Abort as e:
        reason = str(e)
    except Exception:
        log.exception("[broadcast] crashed")
        reason = "unexpected error, check logs"
    finally:
        ctx.application.bot_data["broadcast_running"] = False

    head = "<b>broadcast stopped ❌</b>" if reason else "<b>broadcast complete ✅</b>"
    body = T(
        "\n\nprocessed: {p}/{t}\nsent: {s}\nfailed: {f}\nmarked inactive: {d}",
        p=done, t=total, s=sent, f=failed, d=dead,
    )
    why = T("\nreason: {r}", r=esc(reason)) if reason else ""
    await _edit(status, f"{T(head)}{body}{why}\n\n{T('by {m}', m=mention(owner))}")


async def broadcast_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    # 1) owner check — silently ignore everyone else
    if not user or user.id != BROADCAST_OWNER_ID:
        return
    # 2) must reply to the message that should be broadcast
    src = msg.reply_to_message
    if not src:
        await say(ctx, chat.id, T("reply to the message you want to broadcast with /broadcast."),
                  reply_to=msg.message_id)
        return
    # 3) one broadcast at a time
    if ctx.application.bot_data.get("broadcast_running"):
        await say(ctx, chat.id, T("a broadcast is already running, wait for it to finish."),
                  reply_to=msg.message_id)
        return
    # 4) collect targets: groups first, then users
    chats = await dbase.get_broadcast_chats()
    users = await dbase.get_broadcast_users()
    targets = [("chats", c) for c in chats] + [("users", u) for u in users]
    if not targets:
        await say(ctx, chat.id, T("no active users or chats found."), reply_to=msg.message_id)
        return

    ctx.application.bot_data["broadcast_running"] = True
    status = await say(
        ctx, chat.id,
        T("<b>broadcasting...</b>\n\nprocessed: {p}/{t} | sent: {s} | failed: {f}",
          p=0, t=len(targets), s=0, f=0),
        reply_to=msg.message_id,
    )
    # run in the background: PTB handles updates one by one by default, so awaiting a long
    # loop here would freeze the whole bot until the broadcast ends.
    ctx.application.create_task(_run(ctx, status, user, chat.id, src.message_id, targets))


def register(app):
    # group_only=True → the command is ignored in the bot's DM
    dual_command(app, "broadcast", broadcast_cmd, group_only=True)
  
