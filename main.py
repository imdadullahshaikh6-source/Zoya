# -*- coding: utf-8 -*-
"""
main.py — Telegram bot with a /promote command that opens a custom-styled
web page (colorful buttons + stylized font, like the "Duke" bot reference)
instead of plain Telegram inline buttons.

Why a web page: Telegram's own inline keyboard buttons cannot be recolored
or restyled — that's a Telegram client limitation, not something any bot
can change. A custom look is only possible via a small web page that opens
when the admin taps a link, which is what this file serves.

Secrets (BOT_TOKEN, MONGO_URI, WEBAPP_URL) are loaded from environment
variables / a local .env file — NEVER hardcode them here, and NEVER commit
.env to GitHub (keep it in .gitignore).

Local setup:
    pip install -r requirements.txt
    cp .env.example .env      # fill in your real token/uri/webapp url
    python3 main.py

On Railway:
    1. Deploy this repo as usual.
    2. Settings → Networking → "Generate Domain" (gives a public https URL).
    3. Variables tab → add WEBAPP_URL = that generated URL (no trailing slash).
    4. Redeploy. Railway sets PORT automatically — this file reads it.
"""

import asyncio
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone

from aiohttp import web
from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import Application, CommandHandler, ContextTypes

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "telegram_bot")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing. Set it in your .env file (see .env.example).")
if not WEBAPP_URL:
    raise SystemExit(
        "WEBAPP_URL missing. On Railway: Settings -> Networking -> Generate Domain, "
        "then add that URL as WEBAPP_URL in Variables."
    )

# --------------------------------------------------------------------------
# Permission catalogue
# --------------------------------------------------------------------------
PERMISSIONS = [
    ("can_change_info", "Change Info"),
    ("can_delete_messages", "Delete Msgs"),
    ("can_invite_users", "Invite Users"),
    ("can_restrict_members", "Ban Users"),
    ("can_pin_messages", "Pin Msgs"),
    ("can_manage_video_chats", "Manage VC"),
    ("can_manage_chat", "Manage Chat"),
    ("is_anonymous", "Anonymous"),
    ("can_promote_members", "Add Admins"),
    ("can_manage_topics", "Manage Topics"),
]

# token -> {chat_id, target_id, target_name, actor_id, created}
PENDING: dict[str, dict] = {}
TOKEN_TTL_SECONDS = 15 * 60


def _prune_expired() -> None:
    now = time.time()
    dead = [t for t, s in PENDING.items() if now - s["created"] > TOKEN_TTL_SECONDS]
    for t in dead:
        PENDING.pop(t, None)


def _save_promotion(mongo_db, *, chat_id, actor_id, actor_name, target_id, target_name, rights):
    if mongo_db is None:
        logger.warning("No MongoDB database configured, skipping save.")
        return
    try:
        mongo_db["promotions"].insert_one(
            {
                "chat_id": chat_id,
                "actor_id": actor_id,
                "actor_name": actor_name,
                "target_id": target_id,
                "target_name": target_name,
                "rights": rights,
                "timestamp": datetime.now(timezone.utc),
            }
        )
    except Exception:
        logger.exception("Failed to save promotion record to MongoDB")


# --------------------------------------------------------------------------
# /promote command — sends a link to the styled web panel
# --------------------------------------------------------------------------
async def cmd_promote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return

    actor = update.effective_user
    actor_member = await chat.get_member(actor.id)
    if actor_member.status not in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
        await message.reply_text("Only group admins can promote members.")
        return

    if not message.reply_to_message:
        await message.reply_text("Reply to the user's message with /promote to start.")
        return

    target = message.reply_to_message.from_user
    if target.is_bot:
        await message.reply_text("Bots can't be promoted this way.")
        return

    _prune_expired()
    token = secrets.token_urlsafe(16)
    PENDING[token] = {
        "chat_id": chat.id,
        "target_id": target.id,
        "target_name": target.full_name,
        "actor_id": actor.id,
        "created": time.time(),
    }

    link = f"{WEBAPP_URL}/promote-app?token={token}"
    await message.reply_text(
        f"Opening the admin-rights panel for <b>{target.full_name}</b>.\n"
        f"Tap below (link expires in 15 minutes):",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛡️ Open Promote Panel", url=link)]]
        ),
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Bot is up. Reply to a member's message with /promote in a group to grant admin rights."
    )


# --------------------------------------------------------------------------
# Web panel (this is what gives the colorful custom look)
# --------------------------------------------------------------------------
PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Promote {target_name}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=MedievalSharp&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #10151d;
    --card: #1b2330;
    --red-bg: #3a2430;
    --red-text: #e37a86;
    --green-bg: #1f3a2c;
    --green-text: #6fdc9a;
    --blue-accent: #4aa3ff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    min-height: 100vh;
    background: var(--bg);
    font-family: 'MedievalSharp', 'Trebuchet MS', sans-serif;
    color: #eee;
    padding: 20px 14px 40px;
    padding-top: max(20px, env(safe-area-inset-top));
    padding-bottom: max(40px, env(safe-area-inset-bottom));
  }}
  .card {{
    max-width: 480px;
    margin: 0 auto;
    background: var(--card);
    border-radius: 16px;
    padding: 18px;
  }}
  h1 {{
    font-size: 20px;
    margin: 0 0 14px;
    color: #f2c6cc;
  }}
  .banner {{
    border-left: 3px solid var(--blue-accent);
    background: #223047;
    padding: 10px 12px;
    border-radius: 8px;
    font-size: 15px;
    margin-bottom: 18px;
    line-height: 1.4;
  }}
  .banner b {{ color: #9ecbff; }}
  .grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    margin-bottom: 14px;
  }}
  .perm {{
    background: var(--red-bg);
    color: var(--red-text);
    border: none;
    border-radius: 12px;
    padding: 14px 8px;
    font-family: inherit;
    font-size: 15px;
    cursor: pointer;
    transition: transform .08s ease, opacity .15s ease;
  }}
  .perm.on {{
    opacity: 1;
    box-shadow: inset 0 0 0 2px var(--red-text);
  }}
  .perm:active {{ transform: scale(0.97); }}
  .confirm-btn {{
    width: 100%;
    background: var(--green-bg);
    color: var(--green-text);
    border: none;
    border-radius: 12px;
    padding: 16px;
    font-family: inherit;
    font-size: 17px;
    cursor: pointer;
    margin-top: 6px;
  }}
  .confirm-btn:disabled {{ opacity: 0.5; }}
  .status {{
    margin-top: 14px;
    font-size: 14px;
    text-align: center;
    min-height: 18px;
  }}
  .status.ok {{ color: var(--green-text); }}
  .status.err {{ color: var(--red-text); }}
</style>
</head>
<body>
  <div class="card">
    <h1>🛡️ Promote Panel</h1>
    <div class="banner">Select admin rights for <b>{target_name}</b>.</div>
    <div class="grid" id="grid"></div>
    <button class="confirm-btn" id="confirmBtn">✅ Confirm</button>
    <div class="status" id="status"></div>
  </div>

<script>
  const PERMS = {perms_json};
  const TOKEN = {token_json};
  const state = {{}};
  PERMS.forEach(p => state[p[0]] = false);

  const grid = document.getElementById('grid');
  function render() {{
    grid.innerHTML = '';
    PERMS.forEach(([key, label]) => {{
      const btn = document.createElement('button');
      btn.className = 'perm' + (state[key] ? ' on' : '');
      btn.textContent = (state[key] ? '✅ ' : '▫️ ') + label;
      btn.onclick = () => {{ state[key] = !state[key]; render(); }};
      grid.appendChild(btn);
    }});
  }}
  render();

  const statusEl = document.getElementById('status');
  document.getElementById('confirmBtn').onclick = async (e) => {{
    e.target.disabled = true;
    statusEl.textContent = 'Promoting...';
    statusEl.className = 'status';
    try {{
      const res = await fetch('/promote-app/confirm', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{token: TOKEN, rights: state}})
      }});
      const data = await res.json();
      if (data.ok) {{
        statusEl.textContent = data.message;
        statusEl.className = 'status ok';
      }} else {{
        statusEl.textContent = data.message;
        statusEl.className = 'status err';
        e.target.disabled = false;
      }}
    }} catch (err) {{
      statusEl.textContent = 'Network error, try again.';
      statusEl.className = 'status err';
      e.target.disabled = false;
    }}
  }};
</script>
</body>
</html>
"""


async def handle_promote_app(request: web.Request) -> web.Response:
    _prune_expired()
    token = request.query.get("token", "")
    session = PENDING.get(token)
    if not session:
        return web.Response(
            text="<h2 style='font-family:sans-serif;color:#eee;background:#10151d;padding:40px;'>"
            "This link has expired. Run /promote again in the group.</h2>",
            content_type="text/html",
            status=404,
        )

    html = PAGE_TEMPLATE.format(
        target_name=session["target_name"],
        perms_json=json.dumps(PERMISSIONS),
        token_json=json.dumps(token),
    )
    return web.Response(text=html, content_type="text/html")


async def handle_promote_confirm(request: web.Request) -> web.Response:
    _prune_expired()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "message": "Bad request."}, status=400)

    token = body.get("token", "")
    rights_in = body.get("rights", {})
    session = PENDING.get(token)
    if not session:
        return web.json_response({"ok": False, "message": "Link expired. Run /promote again."}, status=410)

    application: Application = request.app["application"]
    bot = application.bot
    mongo_db = application.bot_data.get("mongo_db")

    chat_id = session["chat_id"]
    target_id = session["target_id"]
    actor_id = session["actor_id"]

    # re-check the actor is still an admin at confirm time
    try:
        actor_member = await bot.get_chat_member(chat_id, actor_id)
    except (BadRequest, Forbidden):
        PENDING.pop(token, None)
        return web.json_response({"ok": False, "message": "Could not verify admin status."}, status=403)

    if actor_member.status not in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
        PENDING.pop(token, None)
        return web.json_response({"ok": False, "message": "You're no longer an admin here."}, status=403)

    rights = {key: bool(rights_in.get(key, False)) for key, _ in PERMISSIONS}

    try:
        await bot.promote_chat_member(
            chat_id=chat_id,
            user_id=target_id,
            can_change_info=rights["can_change_info"],
            can_delete_messages=rights["can_delete_messages"],
            can_invite_users=rights["can_invite_users"],
            can_restrict_members=rights["can_restrict_members"],
            can_pin_messages=rights["can_pin_messages"],
            can_manage_video_chats=rights["can_manage_video_chats"],
            can_manage_chat=rights["can_manage_chat"],
            is_anonymous=rights["is_anonymous"],
            can_promote_members=rights["can_promote_members"],
            can_manage_topics=rights["can_manage_topics"],
        )
    except (BadRequest, Forbidden) as e:
        return web.json_response(
            {"ok": False, "message": f"Failed to promote: {getattr(e, 'message', e)}"}, status=400
        )

    actor_name = actor_member.user.full_name
    target_name = session["target_name"]

    _save_promotion(
        mongo_db,
        chat_id=chat_id,
        actor_id=actor_id,
        actor_name=actor_name,
        target_id=target_id,
        target_name=target_name,
        rights=rights,
    )

    granted = [label for key, label in PERMISSIONS if rights[key]]
    granted_text = ", ".join(granted) if granted else "no special rights (admin badge only)"

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"✅ {target_name} promoted by {actor_name} with: {granted_text}",
        )
    except (BadRequest, Forbidden):
        logger.warning("Could not post confirmation message in chat %s", chat_id)

    PENDING.pop(token, None)
    return web.json_response({"ok": True, "message": f"Promoted! Granted: {granted_text}"})


# --------------------------------------------------------------------------
# Entry point — runs the bot (polling) and the web panel together
# --------------------------------------------------------------------------
async def run() -> None:
    mongo_client = MongoClient(MONGO_URI)
    mongo_db = mongo_client[MONGO_DB_NAME]
    try:
        mongo_client.admin.command("ping")
        logger.info("Connected to MongoDB (%s)", MONGO_DB_NAME)
    except Exception:
        logger.exception("Could not connect to MongoDB — check MONGO_URI")
        raise

    application = Application.builder().token(BOT_TOKEN).build()
    application.bot_data["mongo_db"] = mongo_db
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("promote", cmd_promote))

    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Bot polling started.")

    web_app = web.Application()
    web_app["application"] = application
    web_app.router.add_get("/promote-app", handle_promote_app)
    web_app.router.add_post("/promote-app/confirm", handle_promote_confirm)
    web_app.router.add_get("/", lambda r: web.Response(text="Bot is running."))

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Web panel listening on port %s", PORT)

    await asyncio.Event().wait()  # run forever


if __name__ == "__main__":
    asyncio.run(run())
    
