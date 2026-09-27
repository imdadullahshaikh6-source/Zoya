# -*- coding: utf-8 -*-
"""
bot.py — main entry point.

Starts the Telegram bot, connects to MongoDB, and registers the
/promote feature from promote.py. Run this file to run the bot.
"""

import logging
import os

from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from promote import register_promote_handlers

load_dotenv()  # reads variables from a .env file in the same folder

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "telegram_bot")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing. Set it in your .env file (see .env.example).")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Bot is up. Reply to a member's message with /promote in a group to grant admin rights."
    )


def main() -> None:
    mongo_client = MongoClient(MONGO_URI)
    mongo_db = mongo_client[MONGO_DB_NAME]

    # quick connection check so failures show up immediately, not on first use
    try:
        mongo_client.admin.command("ping")
        logger.info("Connected to MongoDB (%s)", MONGO_DB_NAME)
    except Exception:
        logger.exception("Could not connect to MongoDB — check MONGO_URI in .env")
        raise

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    register_promote_handlers(application, mongo_db)

    logger.info("Bot starting (polling)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
  
