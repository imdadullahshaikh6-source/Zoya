"""MongoDB layer. Everything persists across restarts/redeploys:
welcome messages, rules, AFK status, bans, mutes, and warns."""
import logging
import time

from motor.motor_asyncio import AsyncIOMotorClient

log = logging.getLogger("database")

_db = None


async def init(uri: str, name: str):
    global _db
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=15000)
    _db = client[name]
    await client.admin.command("ping")  # fail fast on a bad URI / network access
    await _db.users.create_index("username")
    await _db.moderation.create_index([("chat_id", 1), ("user_id", 1)])
    await _db.warns.create_index([("chat_id", 1), ("user_id", 1)])
    await _db.members.create_index("chat_id")
    await _db.filters.create_index([("chat_id", 1), ("keyword", 1)], unique=True)
    log.info("MongoDB connected (db: %s)", name)


# ───────── group settings: welcome, clean flags, rules ─────────
async def cfg_get(chat_id: int) -> dict:
    return await _db.chats.find_one({"_id": chat_id}) or {}


async def cfg_set(chat_id: int, **fields):
    await _db.chats.update_one({"_id": chat_id}, {"$set": fields}, upsert=True)


async def save_welcome(chat_id: int, welcome: dict):
    await cfg_set(chat_id, welcome=welcome, welcome_on=True)


async def reset_welcome(chat_id: int):
    await _db.chats.update_one({"_id": chat_id}, {"$unset": {"welcome": ""}}, upsert=True)


# ───────── users (so /promote @username, .ban @username etc. work) ─────────
async def save_user(user_id: int, name: str, username: str | None = None):
    doc = {"name": name}
    if username:
        doc["username"] = username.lower()
    await _db.users.update_one({"_id": user_id}, {"$set": doc}, upsert=True)


async def get_user_id_by_username(username: str):
    doc = await _db.users.find_one({"username": username.lower().lstrip("@")})
    return doc["_id"] if doc else None


# ───────── AFK (global per user) ─────────
async def afk_set(user_id: int, reason: str, chat_id: int | None = None):
    await _db.afk.update_one(
        {"_id": user_id},
        {"$set": {"reason": reason, "since": time.time(), "chat_id": chat_id}},
        upsert=True,
    )


async def afk_get(user_id: int):
    return await _db.afk.find_one({"_id": user_id})


async def afk_clear(user_id: int):
    await _db.afk.delete_one({"_id": user_id})


# ───────── moderation: ban / mute (one active record per chat+user) ─────────
def _mid(chat_id, user_id):
    return f"{chat_id}:{user_id}"


async def mod_set(chat_id: int, user_id: int, kind: str, admin_id: int, reason: str = ""):
    await _db.moderation.update_one(
        {"_id": _mid(chat_id, user_id)},
        {"$set": {
            "chat_id": chat_id, "user_id": user_id, "type": kind,
            "admin_id": admin_id, "reason": reason, "since": time.time(),
        }},
        upsert=True,
    )


async def mod_get(chat_id: int, user_id: int):
    return await _db.moderation.find_one({"_id": _mid(chat_id, user_id)})


async def mod_clear(chat_id: int, user_id: int):
    await _db.moderation.delete_one({"_id": _mid(chat_id, user_id)})


# ───────── warns ─────────
async def warn_add(chat_id: int, user_id: int, admin_id: int, reason: str = "") -> int:
    doc = await _db.warns.find_one_and_update(
        {"_id": _mid(chat_id, user_id)},
        {
            "$inc": {"count": 1},
            "$push": {"reasons": reason or "no reason given"},
            "$set": {"chat_id": chat_id, "user_id": user_id, "last_admin": admin_id},
        },
        upsert=True,
        return_document=True,
    )
    return doc["count"]


async def warn_get(chat_id: int, user_id: int):
    return await _db.warns.find_one({"_id": _mid(chat_id, user_id)})


async def warn_clear(chat_id: int, user_id: int):
    await _db.warns.delete_one({"_id": _mid(chat_id, user_id)})


# ───────── chat members seen (for .waifu / .couple games) ─────────
async def mark_member(chat_id: int, user_id: int, name: str):
    await _db.members.update_one(
        {"_id": f"{chat_id}:{user_id}"},
        {"$set": {"chat_id": chat_id, "user_id": user_id, "name": name}},
        upsert=True,
    )


async def random_members(chat_id: int, count: int):
    cursor = _db.members.aggregate([
        {"$match": {"chat_id": chat_id}},
        {"$sample": {"size": count}},
    ])
    return [doc async for doc in cursor]


# ───────── kang packs (one growing sticker pack per user) ─────────
async def kang_get(user_id: int):
    return await _db.kangs.find_one({"_id": user_id})


async def kang_set(user_id: int, name: str, count: int, part: int | None = None):
    fields = {"name": name, "count": count}
    if part is not None:
        fields["part"] = part
    await _db.kangs.update_one({"_id": user_id}, {"$set": fields}, upsert=True)


# ───────── filters (keyword -> auto reply, per chat) ─────────
async def filter_set(chat_id: int, keyword: str, data: dict):
    """Save or overwrite a filter for a chat."""
    keyword = keyword.lower()
    doc = {
        "chat_id": chat_id,
        "keyword": keyword,
        "type": data.get("type", "text"),
        "content": data.get("content", ""),
        "caption": data.get("caption", ""),
        "buttons": data.get("buttons"),  # list of rows of {text, url} dicts
        "set_by": data.get("set_by"),
        "since": time.time(),
    }
    await _db.filters.update_one(
        {"chat_id": chat_id, "keyword": keyword},
        {"$set": doc},
        upsert=True,
    )


async def filter_get(chat_id: int, keyword: str):
    """Return a single filter doc, or None."""
    return await _db.filters.find_one({"chat_id": chat_id, "keyword": keyword.lower()})


async def filter_delete(chat_id: int, keyword: str) -> int:
    """Delete a specific filter. Returns deleted count."""
    res = await _db.filters.delete_one({"chat_id": chat_id, "keyword": keyword.lower()})
    return res.deleted_count


async def filter_delete_all(chat_id: int) -> int:
    """Delete every filter in a chat. Returns deleted count."""
    res = await _db.filters.delete_many({"chat_id": chat_id})
    return res.deleted_count


async def filter_list(chat_id: int):
    """Return all filters for a chat."""
    cursor = _db.filters.find({"chat_id": chat_id}).sort("keyword", 1)
    return [doc async for doc in cursor]


async def filter_count(chat_id: int) -> int:
    return await _db.filters.count_documents({"chat_id": chat_id})
            
