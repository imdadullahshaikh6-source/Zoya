"""MongoDB layer: welcome messages and all group settings are stored here,
so nothing is lost when the bot restarts or redeploys on Railway."""
import logging

from motor.motor_asyncio import AsyncIOMotorClient

log = logging.getLogger("database")

_db = None


async def init(uri: str, name: str):
    global _db
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=15000)
    _db = client[name]
    await client.admin.command("ping")  # fail fast if URI / network access is wrong
    await _db.users.create_index("username")
    log.info("MongoDB connected (db: %s)", name)


# ───────── group settings (welcome, clean flags, rules, last welcome id) ─────────
async def cfg_get(chat_id: int) -> dict:
    return await _db.chats.find_one({"_id": chat_id}) or {}


async def cfg_set(chat_id: int, **fields):
    await _db.chats.update_one({"_id": chat_id}, {"$set": fields}, upsert=True)


async def save_welcome(chat_id: int, welcome: dict):
    """welcome = {"type", "file_id", "text"} — stored permanently."""
    await cfg_set(chat_id, welcome=welcome, welcome_on=True)


async def reset_welcome(chat_id: int):
    await _db.chats.update_one({"_id": chat_id}, {"$unset": {"welcome": ""}}, upsert=True)


# ───────── users (for /promote @username and DM starters) ─────────
async def save_user(user_id: int, name: str, username: str | None = None):
    doc = {"name": name}
    if username:
        doc["username"] = username.lower()
    await _db.users.update_one({"_id": user_id}, {"$set": doc}, upsert=True)


async def get_user_id_by_username(username: str):
    doc = await _db.users.find_one({"username": username.lower().lstrip("@")})
    return doc["_id"] if doc else None
  
