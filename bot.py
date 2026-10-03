"""Entry point: creates the bot, wires up every plugin, and handles the
DM /start experience (reaction + photo + buttons)."""
import asyncio
import logging
import os

from dotenv import load_dotenv
load_dotenv()

import aiofastnet
from telegram import InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, Defaults,
    MessageHandler, filters,
)

import admin
import afk
import ban
import cleancommand
import database as dbase
import filters as bot_filters
import fun
import guardian
import lock
import pin
import ping
import sticker
import welcome
import mantion
from common import B, T, log, mention, say, sc

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
DB_NAME = os.getenv("DB_NAME", "zoya_bot")
OWNER_USERNAME = "Ownerbackk"
OWNER_URL = f"https://t.me/{OWNER_USERNAME}"
CHANNEL_URL = os.getenv("CHANNEL_URL", "")
START_IMG = os.getenv("START_IMG", "https://graph.org/file/d3a2c17942e606f4ec811-9c0373fa8bb10f4448.jpg")

START_TXT = (
    "<b>✦ hey {m} !</b>\n\n"
    "i am <b>𝙕𝙤𝙮𝙖</b> — a powerful group management bot.\n"
    "➤ stylish welcome messages with buttons\n"
    "➤ promote / demote with a live power panel\n"
    "➤ ban, mute, warn — each with an undo button\n"
    "➤ afk tracking\n"
    "➤ guardian — anti-edit & anti-media defender\n"
    "➤ locks — auto-delete spam type messages\n"
    "➤ clean command & pin tools\n"
    "➤ every command works with / or . in groups\n\n"
    "tap <b>command</b> below to see everything i can do."
)

GUARDIAN_TXT = (
    "<b>🛡 𝙂𝙪𝙖𝙧𝙙𝙞𝙖𝙣 — Media & Edit Defender</b>\n\n"
    "<b>Features Overview:</b>\n"
    "• Advanced Delayed Edited Message Deletion.\n"
    "• Delayed Media (Photo, Video, Voice, Audio, Doc) Deletion.\n"
    "• Configurable Deletion Timer per chat.\n"
    "• Permit System for trusted users.\n\n"
    "<b>Timer Commands:</b>\n"
    "• <code>.setdelay 5m</code> — set deletion delay to 5 minutes.\n"
    "• <code>.setdelay 6h</code> — set deletion delay to 6 hours.\n"
    "<i>(setdelay range → 1 minute to 6 hours)</i>\n\n"
    "<b>Permit Commands (owner only):</b>\n"
    "• <code>.permit</code> (reply) — whitelist a user (their edits/media won't be deleted).\n"
    "• <code>.unpermit</code> (reply) — remove a user from the permit list.\n"
    "• <code>.permitlist</code> — view permitted users.\n"
    "• <code>.guard on</code> / <code>.guard off</code> — enable/disable Guardian."
)

LOCKS_TXT = (
    "<b>✦ 𝙇𝙤𝙘𝙠𝙨</b>\n\n"
    "Do stickers annoy you? Or want to avoid people sharing links? Or pictures? "
    "You're in the right place!\n\n"
    "The locks module allows you to lock away some common items in the Telegram world; "
    "the bot will automatically delete them!\n\n"
    "<b>Admin commands:</b>\n"
    "• <code>/lock &lt;item(s)&gt;</code>: Lock one or more items. Now, only admins can use this type!\n"
    "• <code>/unlock &lt;item(s)&gt;</code>: Unlock one or more items. Everyone can use this type again!\n"
    "• <code>/locks</code>: List currently locked items.\n"
    "• <code>/locktypes</code>: Show the list of all lockable items."
)

UTILITY_TXT = cleancommand.HELP_TXT + "\n\n" + pin.HELP_TXT

# ✅ FIX: utility aur locks ki position swap kar di
PAGES = {
    "greet": ("🎉 𝙂𝙧𝙚𝙚𝙩𝙞𝙣𝙜𝙨", welcome.HELP_TXT),
    "admin": ("👮 𝘼𝙙𝙢𝙞𝙣", admin.HELP_TXT),
    "afk": ("💤 𝘼𝙁𝙆", afk.HELP_TXT),
    "mod": ("🛡 𝙈𝙤𝙙𝙚𝙧𝙖𝙩𝙞𝙤𝙣", ban.HELP_TXT),
    "extra": ("🎁 𝙀𝙭𝙩𝙧𝙖", sticker.HELP_TXT + "\n\n" + fun.HELP_TXT),
    "filters": ("🔍 𝙁𝙞𝙡𝙩𝙚𝙧𝙨", bot_filters.HELP_TXT),
    "guardian": ("🛡 𝙂𝙪𝙖𝙧𝙙𝙞𝙖𝙣", GUARDIAN_TXT),
    "utility": ("🧰 𝙐𝙩𝙞𝙡𝙞𝙩𝙮", UTILITY_TXT),  # Ab yahan Utility aayega
    "locks": ("🔒 𝙇𝙤𝙘𝙠𝙨", LOCKS_TXT),      # Ab yahan Locks aayega (sabse neeche)
}

ALIASES = {
    "welcome": "greet", "greetings": "greet", "greet": "greet",
    "admin": "admin", "promote": "admin",
    "afk": "afk",
    "mod": "mod", "moderation": "mod", "ban": "mod", "mute": "mod", "warn": "mod", "kick": "mod",
    "extra": "extra", "sticker": "extra", "stickers": "extra", "q": "extra",
    "kang": "extra", "waifu": "extra", "couple": "extra", "fun": "extra",
    "filter": "filters", "filters": "filters", "f": "filters",
    "guardian": "guardian", "defender": "guardian", "setdelay": "guardian", "permit": "guardian",
    "lock": "locks", "locks": "locks", "locktypes": "locks",
    "utility": "utility", "clean": "utility", "cleancommand": "utility",
    "pin": "utility", "unpin": "utility",
}


def add_me_url(username: str) -> str:
    rights = (
        "change_info+delete_messages+restrict_members+invite_users+pin_messages"
        "+manage_video_chats+manage_topics+promote_members+manage_chat"
    )
    return f"https://t.me/{username}?startgroup=true&admin={rights}"


def home_page(user, ctx):
    me = ctx.application.bot_data["me"]
    text = T(START_TXT, m=mention(user))
    rows = [
        [B("📜 𝘾𝙤𝙢𝙢𝙖𝙣𝙙", "help:main", style="primary")],
    ]
    row2 = [B("👑 𝙊𝙬𝙣𝙚𝙧", url=OWNER_URL, style="danger")]
    if CHANNEL_URL:
        row2.append(B("🔔 𝘾𝙝𝙖𝙣𝙣𝙚𝙡", url=CHANNEL_URL, style="danger"))
    rows.append(row2)
    rows.append([B("➕ 𝘼𝙙𝙙 𝙈𝙚", url=add_me_url(me.username), style="success")])
    return text, InlineKeyboardMarkup(rows)


def main_page():
    text = sc("<b>✦ command</b>\n\nchoose a category to see all details.")
    keys = list(PAGES)
    rows = []
    for i in range(0, len(keys), 2):
        row = []
        for j, k in enumerate(keys[i:i + 2]):
            # Agar 'locks' hai toh red, warna alternate green/blue
            if k == "locks":
                btn_style = "danger"
            else:
                btn_style = "success" if (i + j) % 2 == 0 else "primary"
            row.append(B(PAGES[k][0], f"help:{k}", style=btn_style))
        rows.append(row)
    rows.append([B("⬅ 𝘽𝙖𝙘𝙠", "help:home"), B("✖ 𝘾𝙡𝙤𝙨𝙚", "help:close", style="danger")])
    return text, InlineKeyboardMarkup(rows)


def section_page(key):
    label, body = PAGES[key]
    rows = []
    if key == "locks":
        rows.append([B("𝙇𝙤𝙘𝙠𝙩𝙮𝙥𝙚𝙨", "help:locktypes", style="primary")])
    rows.append([B("⬅ 𝘽𝙖𝙘𝙠", "help:main"), B("✖ 𝘾𝙡𝙤𝙨𝙚", "help:close", style="danger")])
    return sc(body), InlineKeyboardMarkup(rows)


async def edit_page(qy, text, kb):
    m = qy.message
    try:
        if m.photo or m.video or m.animation:
            await qy.edit_message_caption(caption=f"<blockquote>{text}</blockquote>", reply_markup=kb)
        else:
            await qy.edit_message_text(f"<blockquote>{text}</blockquote>", reply_markup=kb)
    except TelegramError as e:
        if "not modified" not in str(e).lower():
            raise


async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    me = ctx.application.bot_data["me"]

    if chat.type != ChatType.PRIVATE:
        kb = InlineKeyboardMarkup([[B("💬 Open in DM", url=f"https://t.me/{me.username}?start=help", style="primary")]])
        await say(ctx, chat.id, T("i am alive ✨ open my dm to see all commands."), kb=kb, reply_to=msg.message_id)
        return

    try:
        await ctx.bot.set_message_reaction(chat.id, msg.message_id, reaction="❤")
    except TelegramError:
        pass

    await dbase.save_user(user.id, user.first_name, user.username)

    arg = (ctx.args[0].lower() if ctx.args else "")
    key = ALIASES.get(arg)
    if key:
        text, kb = section_page(key)
        await ctx.bot.send_message(chat.id, f"<blockquote>{text}</blockquote>", parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    text, kb = home_page(user, ctx)
    try:
        await ctx.bot.send_photo(chat.id, START_IMG, caption=f"<blockquote>{text}</blockquote>", parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError as e:
        log.warning("start image failed: %s", e)
        await ctx.bot.send_message(chat.id, f"<blockquote>{text}</blockquote>", parse_mode=ParseMode.HTML, reply_markup=kb)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    arg = (ctx.args[0].lower() if ctx.args else "")
    key = ALIASES.get(arg)
    if chat.type != ChatType.PRIVATE:
        me = ctx.application.bot_data["me"]
        target = f"?start={arg}" if key else "?start=help"
        kb = InlineKeyboardMarkup([[B("💬 Open in DM", url=f"https://t.me/{me.username}{target}", style="primary")]])
        await say(ctx, chat.id, T("open my dm to see the commands."), kb=kb, reply_to=msg.message_id)
        return
    if key:
        text, kb = section_page(key)
    else:
        text, kb = main_page()
    await ctx.bot.send_message(chat.id, f"<blockquote>{text}</blockquote>", parse_mode=ParseMode.HTML, reply_markup=kb)


async def help_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    qy = update.callback_query
    page = qy.data.split(":")[1]
    await qy.answer()
    if page == "close":
        try:
            await qy.message.delete()
        except TelegramError:
            pass
        return
    if page == "home":
        text, kb = home_page(qy.from_user, ctx)
    elif page == "main":
        text, kb = main_page()
    elif page == "locktypes":
        text = sc("<b>The available locktypes are:</b>")
        kb = lock.get_locktypes_kb()
    elif page in PAGES:
        text, kb = section_page(page)
    else:
        return
    await edit_page(qy, text, kb)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=ctx.error)


_seen: dict = {}
_seen_member: dict = {}


async def _track_usernames(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u, chat = update.effective_user, update.effective_chat
    if not u or u.is_bot:
        return
    if chat and chat.type != ChatType.PRIVATE:
        key = (chat.id, u.id)
        if _seen_member.get(key) != u.first_name:
            _seen_member[key] = u.first_name
            await dbase.mark_member(chat.id, u.id, u.first_name)
    if u.username and _seen.get(u.id) != u.username:
        _seen[u.id] = u.username
        await dbase.save_user(u.id, u.first_name, u.username)


async def post_init(app: Application):
    await dbase.init(MONGO_URI, DB_NAME)
    app.bot_data["me"] = await app.bot.get_me()
    cmds = [("start", "Start the bot"), ("help", "Show commands")]
    for mod in (welcome, admin, afk, ban, sticker, fun, bot_filters, ping,
                guardian, cleancommand, pin, lock):
        cmds += mod.COMMANDS
    await app.bot.set_my_commands(cmds)
    log.info("Started as @%s", app.bot_data["me"].username)


def main():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    try:
        aiofastnet.install_policy()
        log.info("aiofastnet installed — faster HTTPS connections 🚀")
    except Exception as e:
        log.warning("aiofastnet install failed, using default loop: %s", e)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(Defaults(parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)))
        .post_init(post_init)
        .build()
    )

    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, _track_usernames), group=-1)

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(help_cb, pattern=r"^help:"))

    welcome.register(app)
    admin.register(app)
    afk.register(app)
    ban.register(app)
    sticker.register(app)
    fun.register(app)
    bot_filters.register(app)
    ping.register(app)
    guardian.register(app)
    cleancommand.register(app)
    pin.register(app)
    mantion.register(app)
    lock.register(app)

    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
