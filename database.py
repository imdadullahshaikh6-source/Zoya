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
    
