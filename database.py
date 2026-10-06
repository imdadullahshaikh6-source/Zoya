"""MongoDB layer. Everything persists across restarts/redeploys."""
import logging
import time

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

    # ✅ SAFE: filters index (handles existing duplicates gracefully)
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

    await _db.guardian.create_index("chat_id", unique=True)
    await _db.clean.create_index("chat_id", unique=True)
    await _db.cleanservice.create_index("chat_id", unique=True)
    await _db.locks.create_index("chat_id", unique=True)
    log.info("MongoDB connected (db: %s)", name)


async def _cleanup_filters_duplicates():
    """Keep only the newest doc per (chat_id, keyword), delete the rest."""
    try:
        pipeline = [
            {"$group": {
                "_id": {"chat_id": "$chat_id", "keyword": "$keyword"},
                "ids": {"$push": "$_id"},
                "count": {"$sum": 1},
            }},
            {"$match": {"count": {"$gt": 1}}},
        ]
        dupes = [doc async for doc in _db.filters.aggregate(pipeline)]
        for d in dupes:
            keep = d["ids"][-1]              # newest doc
            remove = d["ids"][:-1]
            await _db.filters.delete_many({"_id": {"$in": remove}})
            log.info("[db] filters: cleaned %d dupes for %s (kept %s)", len(remove), d["_id"], keep)
    except Exception as e:
        log.error("[db] filters cleanup failed: %s", e)


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


async def kang_get(user_id: int):
    return await _db.kangs.find_one({"_id": user_id})

async def kang_set(user_id: int, name: str, count: int, part: int | None = None):
    fields = {"name": name, "count": count}
    if part is not None:
        fields["part"] = part
    await _db.kangs.update_one({"_id": user_id}, {"$set": fields}, upsert=True)


# ─────────────── filters ───────────────

async def filter_set(chat_id: int, keyword: str, data: dict):
    """Type-safe filter save with logging and strict normalization."""
    if not isinstance(chat_id, int):
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            log.warning("[filter_set] invalid chat_id: %r", chat_id)
            return

    if not keyword:
        log.warning("[filter_set] empty keyword for chat %s", chat_id)
        return

    keyword = keyword.lower().strip()
    doc = {
        "chat_id": chat_id,
        "keyword": keyword,
        "type": data.get("type", "text"),
        "content": data.get("content", ""),
        "caption": data.get("caption", ""),
        "buttons": data.get("buttons"),
        "set_by": data.get("set_by"),
        "since": time.time(),
    }
    result = await _db.filters.update_one(
        {"chat_id": chat_id, "keyword": keyword},
        {"$set": doc},
        upsert=True,
    )
    log.info(
        "[filter_set] chat=%s keyword=%s matched=%d modified=%d upserted=%s",
        chat_id, keyword, result.matched_count, result.modified_count,
        bool(result.upserted_id),
    )


async def filter_get(chat_id: int, keyword: str):
    """Type-safe filter fetch."""
    if not isinstance(chat_id, int):
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            return None
    return await _db.filters.find_one({
        "chat_id": chat_id,
        "keyword": keyword.lower().strip(),
    })


async def filter_delete(chat_id: int, keyword: str) -> int:
    if not isinstance(chat_id, int):
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            return 0
    res = await _db.filters.delete_one({
        "chat_id": chat_id,
        "keyword": keyword.lower().strip(),
    })
    return res.deleted_count


async def filter_delete_all(chat_id: int) -> int:
    if not isinstance(chat_id, int):
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            return 0
    res = await _db.filters.delete_many({"chat_id": chat_id})
    return res.deleted_count


async def filter_list(chat_id: int):
    if not isinstance(chat_id, int):
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            return []
    cursor = _db.filters.find({"chat_id": chat_id}).sort("keyword", 1)
    return [doc async for doc in cursor]


async def filter_count(chat_id: int) -> int:
    if not isinstance(chat_id, int):
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            return 0
    return await _db.filters.count_documents({"chat_id": chat_id})


async def guardian_get(chat_id: int) -> dict | None:
    return await _db.guardian.find_one({"chat_id": chat_id})

async def guardian_set(chat_id: int, delay_seconds: int | None = None, enabled: bool | None = None,
                       edit_delay_seconds: int | None = None, media_delay_seconds: int | None = None):
    upd = {}
    if delay_seconds is not None:
        upd["delay_seconds"] = int(delay_seconds)
    if edit_delay_seconds is not None:
        upd["edit_delay_seconds"] = int(edit_delay_seconds)
    if media_delay_seconds is not None:
        upd["media_delay_seconds"] = int(media_delay_seconds)
    if enabled is not None:
        upd["enabled"] = bool(enabled)
    if upd:
        await _db.guardian.update_one({"chat_id": chat_id}, {"$set": upd}, upsert=True)

async def guardian_permit_add(chat_id: int, user_id: int, name: str):
    await _db.guardian.update_one({"chat_id": chat_id}, {"$pull": {"permitted_users": {"id": user_id}}})
    await _db.guardian.update_one({"chat_id": chat_id}, {"$push": {"permitted_users": {"id": user_id, "name": name}}}, upsert=True)

async def guardian_permit_remove(chat_id: int, user_id: int) -> bool:
    res = await _db.guardian.update_one({"chat_id": chat_id}, {"$pull": {"permitted_users": {"id": user_id}}})
    return res.modified_count > 0


async def clean_get(chat_id: int) -> dict | None:
    return await _db.clean.find_one({"chat_id": chat_id})

async def clean_set(chat_id: int, enabled: bool, mode: str = "all"):
    await _db.clean.update_one({"chat_id": chat_id}, {"$set": {"enabled": bool(enabled), "mode": mode}}, upsert=True)


# ─────────────── clean service (system messages) ───────────────

async def cleanservice_get(chat_id: int) -> dict | None:
    """Fetch cleanservice config. Always returns `enabled` as a strict bool.
    Validates chat_id type so string/int mismatch can't create shadow docs."""
    if not isinstance(chat_id, int):
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            log.warning("[cleanservice_get] invalid chat_id: %r", chat_id)
            return None

    doc = await _db.cleanservice.find_one({"chat_id": chat_id})
    if not doc:
        return None

    # Normalize: always a real Python bool, never int/str/None
    raw = doc.get("enabled")
    if isinstance(raw, str):
        doc["enabled"] = raw.strip().lower() in ("1", "true", "t", "on", "yes", "y")
    else:
        doc["enabled"] = bool(raw)
    return doc


async def cleanservice_set(chat_id: int, enabled: bool):
    """Upsert cleanservice flag. Stores chat_id as int and enabled as strict bool.
    Re-writes chat_id field so legacy string docs get normalized."""
    if not isinstance(chat_id, int):
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            log.warning("[cleanservice_set] invalid chat_id: %r", chat_id)
            return

    # Normalize input to a real bool (handles "on"/"off"/1/0 etc.)
    if isinstance(enabled, str):
        value = enabled.strip().lower() in ("1", "true", "t", "on", "yes", "y")
    else:
        value = bool(enabled)

    result = await _db.cleanservice.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "enabled": value}},
        upsert=True,
    )
    log.info(
        "[cleanservice_set] chat=%s enabled=%s matched=%d modified=%d upserted=%s",
        chat_id, value, result.matched_count, result.modified_count,
        bool(result.upserted_id),
    )


# ───────── locks + approved users ─────────
async def locks_get(chat_id: int) -> dict:
    doc = await _db.locks.find_one({"chat_id": chat_id}) or {}
    approved_raw = doc.get("approved", [])
    approved = {u["id"]: u.get("name", "User") for u in approved_raw}
    return {
        "locks": set(doc.get("locks", [])),
        "unlocks": set(doc.get("unlocks", [])),
        "approved": approved,
    }

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
