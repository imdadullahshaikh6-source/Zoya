"""MongoDB layer. Everything persists across restarts/redeploys."""
import logging
import time
from datetime import datetime, timedelta

from motor.motor_asyncio import AsyncIOMotorClient

log = logging.getLogger("database")

_db = None
db = None


async def init(uri: str, name: str):
    global _db, db
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=15000)
    _db = client[name]
    db = _db
    await client.admin.command("ping")
    await _db.users.create_index("username")
    await _db.moderation.create_index([("chat_id", 1), ("user_id", 1)])
    await _db.warns.create_index([("chat_id", 1), ("user_id", 1)])
    await _db.members.create_index("chat_id")

    try:
        await _db.filters.create_index([("chat_id", 1), ("keyword", 1)], unique=True)
        log.info("[db] filters index ready ✅")
    except Exception as e:
        log.warning("[db] filters index creation failed (%s) — cleaning duplicates...", e)
        await _cleanup_filters_duplicates()
        try:
            await _db.filters.create_index([("chat_id", 1), ("keyword", 1)], unique=True)
            log.info("[db] filters index created after cleanup ✅")
        except Exception as e2:
            log.error("[db] filters index still failed: %s", e2)

    try:
        await _db.guardian.create_index("chat_id", unique=True)
        log.info("[db] guardian index ready ✅")
    except Exception as e:
        log.warning("[db] guardian index creation failed (%s) — cleaning duplicates...", e)
        await _cleanup_guardian_duplicates()
        try:
            await _db.guardian.create_index("chat_id", unique=True)
            log.info("[db] guardian index created after cleanup ✅")
        except Exception as e2:
            log.error("[db] guardian index still failed: %s", e2)

    await _db.clean.create_index("chat_id", unique=True)
    await _db.locks.create_index("chat_id", unique=True)

    try:
        await _db.logchannels.create_index("chat_id", unique=True)
        log.info("[db] logchannels index ready ✅")
    except Exception as e:
        log.warning("[db] logchannels index creation failed: %s", e)

    try:
        await _db.wordseek_scores.create_index([("chat_id", 1), ("score", -1)])
        log.info("[db] wordseek_scores index ready ✅")
    except Exception as e:
        log.warning("[db] wordseek_scores index creation failed: %s", e)

    try:
        await _db.wordseek_history.create_index("chat_id", unique=True)
        log.info("[db] wordseek_history index ready ✅")
    except Exception as e:
        log.warning("[db] wordseek_history index creation failed: %s", e)

    try:
        await _db.kangs.create_index("name")
        log.info("[db] kangs index ready ✅")
    except Exception as e:
        log.warning("[db] kangs index creation failed: %s", e)

    try:
        await _db.blocklist.create_index("chat_id", unique=True)
        log.info("[db] blocklist index ready ✅")
    except Exception as e:
        log.warning("[db] blocklist index creation failed: %s", e)

    log.info("MongoDB connected (db: %s)", name)


async def _cleanup_filters_duplicates():
    try:
        pipeline = [
            {"$group": {"_id": {"chat_id": "$chat_id", "keyword": "$keyword"}, "ids": {"$push": "$_id"}, "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": 1}}},
        ]
        dupes = [doc async for doc in _db.filters.aggregate(pipeline)]
        for d in dupes:
            keep = d["ids"][-1]
            remove = d["ids"][:-1]
            await _db.filters.delete_many({"_id": {"$in": remove}})
            log.info("[db] filters: cleaned %d dupes for %s (kept %s)", len(remove), d["_id"], keep)
    except Exception as e:
        log.error("[db] filters cleanup failed: %s", e)


async def _cleanup_guardian_duplicates():
    try:
        pipeline = [
            {"$group": {"_id": "$chat_id", "ids": {"$push": "$_id"}, "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": 1}}},
        ]
        dupes = [doc async for doc in _db.guardian.aggregate(pipeline)]
        for d in dupes:
            keep = d["ids"][-1]
            remove = d["ids"][:-1]
            await _db.guardian.delete_many({"_id": {"$in": remove}})
            log.info("[db] guardian: cleaned %d dupes for chat_id=%s (kept %s)", len(remove), d["_id"], keep)
    except Exception as e:
        log.error("[db] guardian cleanup failed: %s", e)


async def cfg_get(chat_id: int) -> dict:
    return await _db.chats.find_one({"_id": chat_id}) or {}

async def cfg_set(chat_id: int, **fields):
    await _db.chats.update_one({"_id": chat_id}, {"$set": fields}, upsert=True)

async def save_welcome(chat_id: int, welcome: dict):
    await cfg_set(chat_id, welcome=welcome, welcome_on=True)

async def reset_welcome(chat_id: int):
    await _db.chats.update_one({"_id": chat_id}, {"$unset": {"welcome": ""}}, upsert=True)


async def save_user(user_id: int, name: str, username: str | None = None):
    doc = {"name": name}
    if username:
        doc["username"] = username.lower()
    await _db.users.update_one({"_id": user_id}, {"$set": doc}, upsert=True)

async def get_user_id_by_username(username: str):
    doc = await _db.users.find_one({"username": username.lower().lstrip("@")})
    return doc["_id"] if doc else None


async def afk_set(user_id: int, reason: str, chat_id: int | None = None):
    await _db.afk.update_one({"_id": user_id}, {"$set": {"reason": reason, "since": time.time(), "chat_id": chat_id}}, upsert=True)

async def afk_get(user_id: int):
    return await _db.afk.find_one({"_id": user_id})

async def afk_clear(user_id: int):
    await _db.afk.delete_one({"_id": user_id})


def _mid(chat_id, user_id):
    return f"{chat_id}:{user_id}"

async def mod_set(chat_id: int, user_id: int, kind: str, admin_id: int, reason: str = ""):
    await _db.moderation.update_one({"_id": _mid(chat_id, user_id)}, {"$set": {"chat_id": chat_id, "user_id": user_id, "type": kind, "admin_id": admin_id, "reason": reason, "since": time.time()}}, upsert=True)

async def mod_get(chat_id: int, user_id: int):
    return await _db.moderation.find_one({"_id": _mid(chat_id, user_id)})

async def mod_clear(chat_id: int, user_id: int):
    await _db.moderation.delete_one({"_id": _mid(chat_id, user_id)})


async def warn_add(chat_id: int, user_id: int, admin_id: int, reason: str = "") -> int:
    doc = await _db.warns.find_one_and_update({"_id": _mid(chat_id, user_id)}, {"$inc": {"count": 1}, "$push": {"reasons": reason or "no reason given"}, "$set": {"chat_id": chat_id, "user_id": user_id, "last_admin": admin_id}}, upsert=True, return_document=True)
    return doc["count"]

async def warn_get(chat_id: int, user_id: int):
    return await _db.warns.find_one({"_id": _mid(chat_id, user_id)})

async def warn_clear(chat_id: int, user_id: int):
    await _db.warns.delete_one({"_id": _mid(chat_id, user_id)})


async def mark_member(chat_id: int, user_id: int, name: str):
    await _db.members.update_one({"_id": f"{chat_id}:{user_id}"}, {"$set": {"chat_id": chat_id, "user_id": user_id, "name": name}}, upsert=True)

async def random_members(chat_id: int, count: int):
    cursor = _db.members.aggregate([{"$match": {"chat_id": chat_id}}, {"$sample": {"size": count}}])
    return [doc async for doc in cursor]


# ───────── KANG (single current pack + list of all packs) ─────────

async def kang_get(user_id: int):
    return await _db.kangs.find_one({"_id": user_id})

async def kang_set(user_id: int, name: str, count: int, part: int | None = None, format: str | None = None):
    fields = {"name": name, "count": count}
    if part is not None:
        fields["part"] = part
    if format is not None:
        fields["format"] = format
    await _db.kangs.update_one({"_id": user_id}, {"$set": fields}, upsert=True)


async def kang_list_packs(user_id: int) -> list:
    """Return list of all packs this user has created via .kang."""
    doc = await _db.kangs.find_one({"_id": user_id})
    return doc.get("packs", []) if doc else []


async def kang_upsert_pack(user_id: int, name: str, count: int, part: int, format: str):
    """Add or update a pack entry in the user's pack list."""
    await _db.kangs.update_one({"_id": user_id}, {"$pull": {"packs": {"name": name}}})
    await _db.kangs.update_one(
        {"_id": user_id},
        {"$push": {"packs": {"name": name, "count": count, "part": part, "format": format}}},
        upsert=True,
    )


async def kang_delete_pack(user_id: int, name: str):
    await _db.kangs.update_one({"_id": user_id}, {"$pull": {"packs": {"name": name}}})


# ─────────────── filters ───────────────

async def filter_set(chat_id: int, keyword: str, data: dict):
    if not isinstance(chat_id, int):
        try: chat_id = int(chat_id)
        except (TypeError, ValueError): return
    if not keyword: return
    keyword = keyword.lower().strip()
    doc = {"chat_id": chat_id, "keyword": keyword, "type": data.get("type", "text"), "content": data.get("content", ""), "caption": data.get("caption", ""), "buttons": data.get("buttons"), "set_by": data.get("set_by"), "since": time.time()}
    await _db.filters.update_one({"chat_id": chat_id, "keyword": keyword}, {"$set": doc}, upsert=True)


async def filter_get(chat_id: int, keyword: str):
    if not isinstance(chat_id, int):
        try: chat_id = int(chat_id)
        except (TypeError, ValueError): return None
    return await _db.filters.find_one({"chat_id": chat_id, "keyword": keyword.lower().strip()})


async def filter_delete(chat_id: int, keyword: str) -> int:
    if not isinstance(chat_id, int):
        try: chat_id = int(chat_id)
        except (TypeError, ValueError): return 0
    res = await _db.filters.delete_one({"chat_id": chat_id, "keyword": keyword.lower().strip()})
    return res.deleted_count


async def filter_delete_all(chat_id: int) -> int:
    if not isinstance(chat_id, int):
        try: chat_id = int(chat_id)
        except (TypeError, ValueError): return 0
    res = await _db.filters.delete_many({"chat_id": chat_id})
    return res.deleted_count


async def filter_list(chat_id: int):
    if not isinstance(chat_id, int):
        try: chat_id = int(chat_id)
        except (TypeError, ValueError): return []
    cursor = _db.filters.find({"chat_id": chat_id}).sort("keyword", 1)
    return [doc async for doc in cursor]


async def filter_count(chat_id: int) -> int:
    if not isinstance(chat_id, int):
        try: chat_id = int(chat_id)
        except (TypeError, ValueError): return 0
    return await _db.filters.count_documents({"chat_id": chat_id})


# ─────────────── guardian ───────────────

async def guardian_get(chat_id: int) -> dict | None:
    if not isinstance(chat_id, int):
        try: chat_id = int(chat_id)
        except (TypeError, ValueError): return None
    doc = await _db.guardian.find_one({"chat_id": chat_id})
    if not doc: return None
    for k in ("delay_seconds", "edit_delay_seconds", "media_delay_seconds"):
        if k in doc and doc[k] is not None:
            try: doc[k] = int(doc[k])
            except (TypeError, ValueError): doc[k] = 0
    raw = doc.get("enabled")
    if isinstance(raw, str):
        doc["enabled"] = raw.strip().lower() in ("1", "true", "t", "on", "yes", "y")
    elif raw is None:
        doc["enabled"] = False
    else:
        doc["enabled"] = bool(raw)
    if not isinstance(doc.get("permitted_users"), list):
        doc["permitted_users"] = []
    return doc


async def guardian_set(chat_id: int, delay_seconds: int | None = None, enabled: bool | None = None,
                       edit_delay_seconds: int | None = None, media_delay_seconds: int | None = None):
    if not isinstance(chat_id, int):
        try: chat_id = int(chat_id)
        except (TypeError, ValueError): return
    upd = {}
    if delay_seconds is not None: upd["delay_seconds"] = int(delay_seconds)
    if edit_delay_seconds is not None: upd["edit_delay_seconds"] = int(edit_delay_seconds)
    if media_delay_seconds is not None: upd["media_delay_seconds"] = int(media_delay_seconds)
    if enabled is not None: upd["enabled"] = bool(enabled)
    if not upd: return
    upd["chat_id"] = chat_id
    await _db.guardian.update_one({"chat_id": chat_id}, {"$set": upd}, upsert=True)


async def guardian_permit_add(chat_id: int, user_id: int, name: str):
    if not isinstance(chat_id, int):
        try: chat_id = int(chat_id)
        except (TypeError, ValueError): return
    await _db.guardian.update_one({"chat_id": chat_id}, {"$pull": {"permitted_users": {"id": user_id}}})
    await _db.guardian.update_one({"chat_id": chat_id}, {"$push": {"permitted_users": {"id": user_id, "name": name}}}, upsert=True)

async def guardian_permit_remove(chat_id: int, user_id: int) -> bool:
    if not isinstance(chat_id, int):
        try: chat_id = int(chat_id)
        except (TypeError, ValueError): return False
    res = await _db.guardian.update_one({"chat_id": chat_id}, {"$pull": {"permitted_users": {"id": user_id}}})
    return res.modified_count > 0


async def clean_get(chat_id: int) -> dict | None:
    return await _db.clean.find_one({"chat_id": chat_id})

async def clean_set(chat_id: int, enabled: bool, mode: str = "all"):
    await _db.clean.update_one({"chat_id": chat_id}, {"$set": {"enabled": bool(enabled), "mode": mode}}, upsert=True)


# ───────── locks + approved users ─────────
async def locks_get(chat_id: int) -> dict:
    doc = await _db.locks.find_one({"chat_id": chat_id}) or {}
    approved_raw = doc.get("approved", [])
    approved = {u["id"]: u.get("name", "User") for u in approved_raw}
    return {"locks": set(doc.get("locks", [])), "unlocks": set(doc.get("unlocks", [])), "approved": approved}

async def locks_set_locks(chat_id: int, locks: set):
    await _db.locks.update_one({"chat_id": chat_id}, {"$set": {"locks": list(locks)}}, upsert=True)

async def locks_set_unlocks(chat_id: int, unlocks: set):
    await _db.locks.update_one({"chat_id": chat_id}, {"$set": {"unlocks": list(unlocks)}}, upsert=True)

async def approved_add(chat_id: int, user_id: int, name: str):
    await _db.locks.update_one({"chat_id": chat_id}, {"$pull": {"approved": {"id": user_id}}})
    await _db.locks.update_one({"chat_id": chat_id}, {"$push": {"approved": {"id": user_id, "name": name}}}, upsert=True)

async def approved_remove(chat_id: int, user_id: int) -> bool:
    res = await _db.locks.update_one({"chat_id": chat_id}, {"$pull": {"approved": {"id": user_id}}})
    return res.modified_count > 0

async def approved_list(chat_id: int) -> dict:
    doc = await _db.locks.find_one({"chat_id": chat_id}) or {}
    return {u["id"]: u.get("name", "User") for u in doc.get("approved", [])}


# ─────────────── logchannel ───────────────

async def logchannel_set(chat_id: int, channel_id: int, channel_name: str, set_by: int):
    if not isinstance(chat_id, int):
        try: chat_id = int(chat_id)
        except (TypeError, ValueError): return
    if not isinstance(channel_id, int):
        try: channel_id = int(channel_id)
        except (TypeError, ValueError): return
    await _db.logchannels.update_one(
        {"chat_id": chat_id},
        {"$set": {
            "chat_id": chat_id,
            "channel_id": channel_id,
            "channel_name": channel_name or "Log Channel",
            "set_by": set_by,
            "since": time.time(),
        }},
        upsert=True,
    )
    log.info("[logchannel] set chat=%s -> channel=%s (%s)", chat_id, channel_id, channel_name)


async def logchannel_get(chat_id: int) -> dict | None:
    if not isinstance(chat_id, int):
        try: chat_id = int(chat_id)
        except (TypeError, ValueError): return None
    return await _db.logchannels.find_one({"chat_id": chat_id})


async def logchannel_unset(chat_id: int) -> bool:
    if not isinstance(chat_id, int):
        try: chat_id = int(chat_id)
        except (TypeError, ValueError): return False
    res = await _db.logchannels.delete_one({"chat_id": chat_id})
    return res.deleted_count > 0


async def logchannel_unset_by_channel(channel_id: int) -> int:
    if not isinstance(channel_id, int):
        try: channel_id = int(channel_id)
        except (TypeError, ValueError): return 0
    res = await _db.logchannels.delete_many({"channel_id": channel_id})
    return res.deleted_count


async def logchannel_groups_for_channel(channel_id: int) -> list:
    if not isinstance(channel_id, int):
        try: channel_id = int(channel_id)
        except (TypeError, ValueError): return []
    cursor = _db.logchannels.find({"channel_id": channel_id})
    return [doc async for doc in cursor]


# ─────────────── WordSeek Leaderboard ───────────────

async def wordseek_add_score(chat_id: int, user_id: int, name: str, points: int):
    """Add points to a user's total. Also stores a per-game event for time filtering."""
    if not isinstance(chat_id, int) or not isinstance(user_id, int): return
    _id = f"ws:{chat_id}:{user_id}"
    now = time.time()

    # Update total (upsert)
    await _db.wordseek_scores.update_one(
        {"_id": _id},
        {
            "$inc": {"score": points},
            "$set": {"chat_id": chat_id, "user_id": user_id, "name": name, "last_played": now}
        },
        upsert=True
    )

    # Also log the event for time-based leaderboards
    try:
        await _db.wordseek_events.insert_one({
            "chat_id": chat_id,
            "user_id": user_id,
            "name": name,
            "score": points,
            "createdAt": now,
        })
    except Exception as e:
        log.warning("[wordseek] event insert failed: %s", e)


def _time_filter_start(time_key: str):
    """Return (start_ts, end_ts) for the given time_key, or (None, None) for all."""
    now = datetime.utcnow()
    if time_key == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif time_key == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif time_key == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif time_key == "year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        return None, None
    return start.timestamp(), None


async def wordseek_get_leaderboard(chat_id: int, time_key: str = "all", limit: int = 10):
    """
    Returns list of {name, score} for the given chat.
    time_key: 'today' | 'week' | 'month' | 'year' | 'all'
    """
    if not isinstance(chat_id, int): return []

    if time_key == "all":
        cursor = _db.wordseek_scores.find({"chat_id": chat_id}).sort("score", -1).limit(limit)
        docs = [doc async for doc in cursor]
        return [{"name": d.get("name", "Unknown"), "score": d.get("score", 0)} for d in docs]

    start_ts, _ = _time_filter_start(time_key)
    if start_ts is None:
        cursor = _db.wordseek_scores.find({"chat_id": chat_id}).sort("score", -1).limit(limit)
        docs = [doc async for doc in cursor]
        return [{"name": d.get("name", "Unknown"), "score": d.get("score", 0)} for d in docs]

    # Aggregate events within the time window
    pipeline = [
        {"$match": {"chat_id": chat_id, "createdAt": {"$gte": start_ts}}},
        {"$group": {"_id": "$user_id", "name": {"$last": "$name"}, "score": {"$sum": "$score"}}},
        {"$sort": {"score": -1}},
        {"$limit": limit},
    ]
    try:
        results = [doc async for doc in _db.wordseek_events.aggregate(pipeline)]
        return [{"name": r.get("name", "Unknown"), "score": r.get("score", 0)} for r in results]
    except Exception as e:
        log.warning("[wordseek] leaderboard aggregation failed: %s", e)
        # Fallback: total scores
        cursor = _db.wordseek_scores.find({"chat_id": chat_id}).sort("score", -1).limit(limit)
        docs = [doc async for doc in cursor]
        return [{"name": d.get("name", "Unknown"), "score": d.get("score", 0)} for d in docs]


async def wordseek_get_user_score(chat_id: int, user_id: int) -> int:
    if not isinstance(chat_id, int) or not isinstance(user_id, int): return 0
    _id = f"ws:{chat_id}:{user_id}"
    doc = await _db.wordseek_scores.find_one({"_id": _id})
    return doc.get("score", 0) if doc else 0


# ─────────────── Stats helpers ───────────────

async def wordseek_get_users_count() -> int:
    """Total users who have played WordSeek (unique across all chats)."""
    try:
        return await _db.wordseek_scores.count_documents({})
    except Exception as e:
        log.warning("[stats] users count failed: %s", e)
        return 0


async def wordseek_get_groups_count() -> int:
    """Total unique group chats that have played WordSeek."""
    try:
        ids = await _db.wordseek_scores.distinct("chat_id")
        return len([c for c in ids if isinstance(c, int) and c < 0])
    except Exception as e:
        log.warning("[stats] groups count failed: %s", e)
        return 0


# ─────────────── WordSeek Word History ───────────────

async def wordseek_add_history(chat_id: int, word: str):
    if not isinstance(chat_id, int): return
    await _db.wordseek_history.update_one(
        {"chat_id": chat_id},
        {"$addToSet": {"words": word.lower()}, "$set": {"updated": time.time()}},
        upsert=True
    )

async def wordseek_get_history(chat_id: int) -> list:
    if not isinstance(chat_id, int): return []
    doc = await _db.wordseek_history.find_one({"chat_id": chat_id})
    return doc.get("words", []) if doc else []

async def wordseek_reset_history(chat_id: int, keep_recent: list = None):
    if not isinstance(chat_id, int): return
    if keep_recent:
        await _db.wordseek_history.update_one(
            {"chat_id": chat_id},
            {"$set": {"words": keep_recent, "updated": time.time()}},
            upsert=True
        )
    else:
        await _db.wordseek_history.delete_one({"chat_id": chat_id})


# ─────────────── blocklist ───────────────

def _bl_cid(chat_id):
    if isinstance(chat_id, int):
        return chat_id
    try:
        return int(chat_id)
    except (TypeError, ValueError):
        return None


async def blocklist_get(chat_id: int) -> dict:
    chat_id = _bl_cid(chat_id)
    if chat_id is None: return {"words": [], "mode": "nothing"}
    doc = await _db.blocklist.find_one({"chat_id": chat_id}) or {}
    return {"words": list(doc.get("words", [])), "mode": doc.get("mode", "nothing")}


async def blocklist_add(chat_id: int, words: list):
    chat_id = _bl_cid(chat_id)
    if chat_id is None or not words: return
    await _db.blocklist.update_one(
        {"chat_id": chat_id},
        {"$addToSet": {"words": {"$each": words}}, "$set": {"chat_id": chat_id, "updated": time.time()}},
        upsert=True,
    )


async def blocklist_remove(chat_id: int, words: list) -> int:
    chat_id = _bl_cid(chat_id)
    if chat_id is None or not words: return 0
    res = await _db.blocklist.update_one({"chat_id": chat_id}, {"$pull": {"words": {"$in": words}}})
    return res.modified_count


async def blocklist_clear(chat_id: int) -> int:
    """Remove every word of this chat (mode is kept). Returns how many words were removed."""
    chat_id = _bl_cid(chat_id)
    if chat_id is None: return 0
    doc = await _db.blocklist.find_one({"chat_id": chat_id}) or {}
    n = len(doc.get("words", []))
    await _db.blocklist.update_one({"chat_id": chat_id}, {"$set": {"words": []}})
    return n


async def blocklist_set_mode(chat_id: int, mode: str):
    chat_id = _bl_cid(chat_id)
    if chat_id is None: return
    await _db.blocklist.update_one({"chat_id": chat_id}, {"$set": {"chat_id": chat_id, "mode": mode}}, upsert=True)
    


# ─────────────── broadcast ───────────────
# Both collections key their documents by _id (user_id / chat_id).
# A doc with {"active": False} was marked dead by a previous broadcast and is skipped.
# Docs that never had the "active" field count as active ($ne False matches them).

_BROADCAST_COLS = ("users", "chats")


async def _broadcast_ids(col_name: str) -> list:
    """All non-dead _ids of a collection, de-duplicated, as ints."""
    try:
        cur = _db[col_name].find({"active": {"$ne": False}}, {"_id": 1})
        ids = [d["_id"] async for d in cur if isinstance(d.get("_id"), int)]
        return list(dict.fromkeys(ids))
    except Exception as e:
        log.warning("[broadcast] fetching %s failed: %s", col_name, e)
        return []


async def get_broadcast_users() -> list:
    """Every user id that is still reachable (active is not False)."""
    return await _broadcast_ids("users")


async def get_broadcast_chats() -> list:
    """Every chat id that is still reachable (active is not False)."""
    return await _broadcast_ids("chats")


async def mark_broadcast_dead(col_name: str, chat_id: int) -> bool:
    """Set {"active": False} on one doc of 'users' or 'chats' so the next broadcast skips it."""
    if col_name not in _BROADCAST_COLS:
        log.warning("[broadcast] mark_dead: bad collection %r", col_name)
        return False
    try:
        # no upsert — we never want to create a doc just to mark it dead
        res = await _db[col_name].update_one({"_id": chat_id}, {"$set": {"active": False}})
        return res.matched_count > 0
    except Exception as e:
        log.warning("[broadcast] mark_dead %s/%s failed: %s", col_name, chat_id, e)
        return False
        
