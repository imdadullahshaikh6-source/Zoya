"""All credentials load from the environment — nothing is hardcoded."""
import os

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]              # a SEPARATE bot token — do not reuse Zoya's
STRING_SESSION = os.environ["STRING_SESSION"]     # Pyrogram string session for the assistant userbot
MONGO_URI = os.environ.get("MONGO_URI")           # optional: only used for a small history log
OWNER_ID = int(os.environ["OWNER_ID"])

SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")

QUEUE_PREVIEW = int(os.environ.get("QUEUE_PREVIEW", "10"))
