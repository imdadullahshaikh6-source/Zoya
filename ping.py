"""Debug version of ping.py"""
import logging
import re
import time

try:
    import psutil
    print("✅ PSUTIL LOADED SUCCESSFULLY")
except ImportError:
    print("❌ PSUTIL NOT INSTALLED! Run: pip install psutil")
    raise

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters as tg_filters
import database as dbase

log = logging.getLogger("ping")

OWNER_ID = 8373739674
START_TIME = time.time()
PING_IMAGE = "https://graph.org/file/d3a2c17942e606f4ec811-9c0373fa8bb10f4448.jpg"

COMMANDS = [
    ("ping", "Check bot latency, uptime, system and database stats"),
]

def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)

def _format_bytes(size: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"

async def ping_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # 🔍 DEBUG PRINT - Terminal me dekhna
    print(f"\n🚨 PING COMMAND TRIGGERED BY: {update.effective_user.id} ({update.effective_user.first_name})\n")
    
    # ❌ OWNER CHECK TEMPORARILY HATA DIYA HAI
    # if not update.effective_user or update.effective_user.id != OWNER_ID:
    #     return

    start = time.time()
    msg = update.effective_message

    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory().percent

    mongo_line = "<b>🗄 MongoDB:</b> N/A"
    try:
        db = getattr(dbase, "db", None) or getattr(dbase, "_db", None)
        if db is not None:
            stats = await db.command("dbstats")
            data_size = stats.get("dataSize", 0)
            storage_size = stats.get("storageSize", 0)
            fs_total = stats.get("fsTotalSize")
            fs_used = stats.get("fsUsedSize")
            if fs_total and fs_used:
                used_pct = (fs_used / fs_total) * 100
                free = fs_total - fs_used
                mongo_line = f"<b>🗄 MongoDB:</b> <code>{used_pct:.1f}% used</code> ({_format_bytes(free)} free)"
            else:
                mongo_line = f"<b>🗄 MongoDB:</b> Data <code>{_format_bytes(data_size)}</code>"
    except Exception as e:
        log.error("MongoDB stats failed: %s", e)

    uptime_str = _format_uptime(time.time() - START_TIME)
    latency_ms = (time.time() - start) * 1000

    caption = (
        f"<b>🏓 Pong!</b>\n\n"
        f"<b>📡 Ping:</b> <code>{latency_ms:.2f} ms</code>\n"
        f"<b>⏱ Uptime:</b> <code>{uptime_str}</code>\n"
        f"<b>💻 CPU:</b> <code>{cpu:.1f}%</code>\n"
        f"<b>🧠 RAM:</b> <code>{ram:.1f}%</code>\n"
        f"{mongo_line}"
    )

    try:
        await msg.reply_photo(
            photo=PING_IMAGE,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=msg.message_id,
        )
        print("✅ Photo sent successfully!")
    except Exception as e:
        print(f"❌ Photo failed: {e}")
        log.error("Failed to send ping photo: %s", e)
        try:
            await msg.reply_text(caption, parse_mode=ParseMode.HTML, reply_to_message_id=msg.message_id)
            print("✅ Fallback text sent successfully!")
        except Exception as fallback_err:
            print(f"❌ Fallback text also failed: {fallback_err}")

def register(app: Application):
    app.add_handler(MessageHandler(
        tg_filters.Regex(r"^[./]ping\s*$", flags=re.IGNORECASE),
        ping_cmd,
    ), group=0)
