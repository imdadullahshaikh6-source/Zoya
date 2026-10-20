"""promo.py — owner-only promo broadcasting (background, slow & safe).

Commands (owner only, "/" or "." both work, NOT shown in the "/" menu):
  .promo-users  -> sirf un users ko jinhone bot ko DM me start kiya
  .promo-gc     -> sirf un groups me jaha bot hai
  .promo-all    -> DM + groups dono
  .promo-stop   -> chalta hua broadcast rok do
  .promo-sync   -> purane users/groups ko promo list me import karo (auto bhi hota hai 1 baar)

Message kaise do:
  1) Kisi message ko REPLY karke command do  -> wo message forward hoga
  2) Ya command ke baad text likho: .promo-users Hello friends ...
"""
import asyncio
import logging
import re
import time

from pymongo import UpdateOne
from telegram import MessageEntity, ReplyParameters, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import ChatMemberHandler, ContextTypes, MessageHandler, filters

import database as dbase

log = logging.getLogger("promo")

OWNER_ID = 8373739674
SEND_MODE = "forward"   # "forward" = "Forwarded from" ke sath | "copy" = bina forward tag ke
DELAY = 0.15            # har message ke baad pause (sec) -> load kam rahega
COMMANDS: list = []     # JAAN BUJH KE KHALI — "/" menu me promo nahi dikhega

_CMD_RE = re.compile(r"^[/.]promo-(users|gc|all|stop|sync)(?:@(\w+))?(?=\s|$)", re.I)

_known: set = set()                 # DB writes kam karne ke liye cache
_state: dict = {"task": None, "stop": None}


# ───────────────────────── tracking (sirf DM users + groups) ─────────────────────────

async def _track_private(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Sirf private chat me jo user bot se baat kare wahi promo_users me jayega."""
    u = update.effective_user
    if not u or u.is_bot or u.id in _known:
        return
    _known.add(u.id)
    try:
        await dbase.db.promo_users.update_one(
            {"_id": u.id}, {"$set": {"name": u.first_name, "t": time.time()}}, upsert=True)
    except Exception as e:
        _known.discard(u.id)
        log.warning("[promo] track user failed: %s", e)


async def _track_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or chat.id in _known:
        return
    _known.add(chat.id)
    try:
        await dbase.db.promo_chats.update_one(
            {"_id": chat.id}, {"$set": {"title": chat.title, "t": time.time()}}, upsert=True)
    except Exception as e:
        _known.discard(chat.id)
        log.warning("[promo] track chat failed: %s", e)


async def _my_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Bot add/remove/block hone par list automatic update."""
    ev = update.my_chat_member
    if not ev:
        return
    chat = ev.chat
    active = ev.new_chat_member.status in ("member", "administrator", "creator", "restricted")
    if chat.type == ChatType.PRIVATE:
        coll = dbase.db.promo_users
    elif chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        coll = dbase.db.promo_chats
    else:
        return
    try:
        if active:
            await coll.update_one({"_id": chat.id}, {"$set": {"t": time.time()}}, upsert=True)
            _known.add(chat.id)
        else:
            await coll.delete_one({"_id": chat.id})
            _known.discard(chat.id)
    except Exception as e:
        log.warning("[promo] my_member update failed: %s", e)


# ───────────────────────── purana data import ─────────────────────────

async def _bulk_add(coll, ids):
    ids = list(ids)
    for i in range(0, len(ids), 500):
        ops = [UpdateOne({"_id": x}, {"$setOnInsert": {"t": time.time()}}, upsert=True)
               for x in ids[i:i + 500]]
        await coll.bulk_write(ops, ordered=False)
    return len(ids)


async def _backfill() -> tuple:
    """Purane `users` aur group-ids ko promo list me daalo.
    Jo user kabhi DM me start nahi kiya wo broadcast ke time Forbidden aayega
    aur list se apne aap hat jayega."""
    db = dbase.db
    user_ids = set()
    try:
        async for d in db.users.find({}, {"_id": 1}):
            if isinstance(d["_id"], int) and d["_id"] > 0:
                user_ids.add(d["_id"])
    except Exception as e:
        log.warning("[promo] backfill users failed: %s", e)

    chat_ids = set()
    for coll, field in (("members", "chat_id"), ("wordseek_scores", "chat_id"),
                        ("guardian", "chat_id"), ("locks", "chat_id"),
                        ("logchannels", "chat_id"), ("chats", "_id")):
        try:
            for cid in await db[coll].distinct(field):
                if isinstance(cid, int) and cid < 0:
                    chat_ids.add(cid)
        except Exception as e:
            log.warning("[promo] backfill %s failed: %s", coll, e)

    nu = await _bulk_add(db.promo_users, user_ids) if user_ids else 0
    nc = await _bulk_add(db.promo_chats, chat_ids) if chat_ids else 0
    await db.promo_meta.update_one({"_id": "sync"}, {"$set": {"done": True, "t": time.time()}}, upsert=True)
    return nu, nc


async def _ensure_synced():
    if not await dbase.db.promo_meta.find_one({"_id": "sync"}):
        await _backfill()


# ───────────────────────── sending ─────────────────────────

def _secs(v) -> float:
    return v.total_seconds() if hasattr(v, "total_seconds") else float(v)


async def _deliver(bot, chat_id: int, job: dict):
    if job["text"] is not None:
        await bot.send_message(chat_id, job["text"], entities=job["entities"], parse_mode=None)
    elif SEND_MODE == "copy":
        await bot.copy_message(chat_id, job["from_chat"], job["msg_id"])
    else:
        await bot.forward_message(chat_id, job["from_chat"], job["msg_id"])


async def _migrate(old: int, new: int):
    db = dbase.db
    await db.promo_chats.delete_one({"_id": old})
    await db.promo_chats.update_one({"_id": new}, {"$set": {"t": time.time()}}, upsert=True)
    _known.discard(old)
    _known.add(new)


async def _send_one(bot, target: int, job: dict) -> str:
    """return: 'ok' | 'dead' (list se hatao) | 'fail'"""
    for _ in range(3):
        try:
            await _deliver(bot, target, job)
            return "ok"
        except RetryAfter as e:                      # flood wait -> ruk ke dobara
            await asyncio.sleep(_secs(e.retry_after) + 1)
        except ChatMigrated as e:                    # group supergroup ban gaya
            await _migrate(target, e.new_chat_id)
            target = e.new_chat_id
        except Forbidden:                            # blocked / kicked / deactivated
            return "dead"
        except BadRequest as e:
            m = str(e).lower()
            if "chat not found" in m or "peer_id_invalid" in m or "user not found" in m:
                return "dead"
            return "fail"
        except NetworkError:                         # timeout / network hiccup
            await asyncio.sleep(2)
        except TelegramError as e:
            log.warning("[promo] send to %s failed: %s", target, e)
            return "fail"
    return "fail"


async def _part(bot, coll_name: str, job: dict, stop: asyncio.Event) -> tuple:
    """Ek ek karke DB se fetch -> ek ek ko bhejo (memory/load kam)."""
    coll = dbase.db[coll_name]
    sent = failed = 0
    async for d in coll.find({}, {"_id": 1}).batch_size(100):
        if stop.is_set():
            break
        tid = d["_id"]
        res = await _send_one(bot, tid, job)
        if res == "ok":
            sent += 1
        else:
            failed += 1
            if res == "dead":
                try:
                    await coll.delete_one({"_id": tid})
                except Exception:
                    pass
                _known.discard(tid)
        await asyncio.sleep(DELAY)
    return sent, failed


async def _runner(bot, scope: str, job: dict, chat_id: int, msg_id: int):
    stop = asyncio.Event()
    _state["stop"] = stop
    try:
        await _ensure_synced()
        lines = []
        if scope in ("users", "all"):
            s, f = await _part(bot, "promo_users", job, stop)
            lines.append(f"» USERS : {s} SENT | {f} FAILED")
        if scope in ("gc", "all") and not stop.is_set():
            s, f = await _part(bot, "promo_chats", job, stop)
            lines.append(f"» GROUPS : {s} SENT | {f} FAILED")
        status = "STOPPED" if stop.is_set() else "COMPLETED"
        await bot.send_message(
            chat_id, f"» BROADCAST {status}\n\n" + "\n".join(lines),
            reply_parameters=ReplyParameters(message_id=msg_id, allow_sending_without_message=True))
    except Exception:
        log.exception("[promo] broadcast crashed")
    finally:
        _state["task"] = None
        _state["stop"] = None


# ───────────────────────── command handler ─────────────────────────

async def promo_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, user = update.effective_message, update.effective_user
    if not msg or not user or user.id != OWNER_ID:      # sirf owner
        return
    m = _CMD_RE.match(msg.text or "")
    if not m:
        return
    scope, mention = m.group(1).lower(), m.group(2)
    if mention and mention.lower() != (ctx.bot.username or "").lower():
        return                                           # kisi aur bot ke liye hai

    running = _state["task"] is not None and not _state["task"].done()

    if scope == "stop":
        if running and _state["stop"]:
            _state["stop"].set()
            await msg.reply_text("» STOPPING BROADCAST...")
        else:
            await msg.reply_text("» NO BROADCAST RUNNING")
        return

    if scope == "sync":
        nu, nc = await _backfill()
        await msg.reply_text(f"» SYNC DONE\n\n» USERS : {nu}\n» GROUPS : {nc}")
        return

    if running:
        await msg.reply_text("» BROADCAST ALREADY RUNNING\n» USE .PROMO-STOP TO CANCEL")
        return

    rest = (msg.text or "")[m.end():]
    body = rest.lstrip()
    reply = msg.reply_to_message
    if reply:
        job = {"text": None, "entities": None, "from_chat": reply.chat_id, "msg_id": reply.message_id}
    elif body:
        offset = m.end() + (len(rest) - len(body))
        ents = [
            MessageEntity(type=e.type, offset=e.offset - offset, length=e.length, url=e.url,
                          user=e.user, language=e.language, custom_emoji_id=e.custom_emoji_id)
            for e in (msg.entities or []) if e.offset >= offset
        ]
        job = {"text": body, "entities": ents or None, "from_chat": None, "msg_id": None}
    else:
        await msg.reply_text("» REPLY TO A MESSAGE OR WRITE TEXT AFTER THE COMMAND")
        return

    await msg.reply_text("» STARTED BROADCASTING...")
    _state["task"] = asyncio.create_task(_runner(ctx.bot, scope, job, msg.chat_id, msg.message_id))


# ───────────────────────── register ─────────────────────────

def register(app):
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE, _track_private, block=False), group=-2)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, _track_group, block=False), group=-2)
    app.add_handler(ChatMemberHandler(_my_member, ChatMemberHandler.MY_CHAT_MEMBER), group=-2)
    app.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & filters.TEXT & filters.Regex(_CMD_RE),
            promo_cmd,
        ),
        group=-3,
    )
