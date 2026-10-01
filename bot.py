"""Entry point: creates the bot, wires up every plugin, and handles the
DM /start experience (reaction + photo + buttons)."""
import logging
import os

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
import database as dbase
import filters as bot_filters
import fun
import sticker
import welcome
from common import B, T, log, mention, say, sc

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
DB_NAME = os.getenv("DB_NAME", "zoya_bot")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "Ownerbackk").lstrip("@")
SUPPORT_URL = os.getenv("SUPPORT_URL", f"https://t.me/{OWNER_USERNAME}")
CHANNEL_URL = os.getenv("CHANNEL_URL", "")
START_IMG = os.getenv("START_IMG", "https://graph.org/file/d3a2c17942e606f4ec811-9c0373fa8bb10f4448.jpg")

START_TXT = (
    "<b>✦ hey {m} !</b>\n\n"
    "i am <b>𝙕𝙤𝙮𝙖</b> — a powerful group management bot.\n"
    "➤ stylish welcome messages with buttons\n"
    "➤ promote / demote with a live power panel\n"
    "➤ ban, mute, warn — each with an undo button\n"
    "➤ afk tracking\n"
    "➤ every command works with / or . in groups\n\n"
    "tap <b>command</b> below to see everything i can do."
)

PAGES = {
    "greet": ("🎉 Greetings", welcome.HELP_TXT),
    "admin": ("👮 Admin", admin.HELP_TXT),
    "afk": ("💤 AFK", afk.HELP_TXT),
    "mod": ("🛡 Moderation", ban.HELP_TXT),
    "extra": ("🎁 Extra", sticker.HELP_TXT + "\n\n" + fun.HELP_TXT),
    "filters": ("🔍 Filters", bot_filters.HELP_TXT),
}

ALIASES = {
    "welcome": "greet", "greetings": "greet", "greet": "greet",
    "admin": "admin", "promote": "admin",
    "afk": "afk",
    "mod": "mod", "moderation": "mod", "ban": "mod", "mute": "mod", "warn": "mod", "kick": "mod",
    "extra": "extra", "sticker": "extra", "stickers": "extra", "q": "extra",
    "kang": "extra", "waifu": "extra", "couple": "extra", "fun": "extra",
    "filter": "filters", "filters": "filters", "f": "filters",
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
        [B("📜 Command", "help:main", style="primary")],
        [B("👑 Support", url=SUPPORT_URL, style="primary"), B("➕ Add Me", url=add_me_url(me.username), style="success")],
    ]
    if CHANNEL_URL:
        rows.append([B("🔔 Channel", url=CHANNEL_URL)])
    return text, InlineKeyboardMarkup(rows)


def main_page():
    text = sc("<b>✦ command</b>\n\nchoose a category to see all details.")
    keys = list(PAGES)
    rows, colors = [], ["success", "primary", "success", "primary"]
    for i in range(0, len(keys), 2):
        row = []
        for j, k in enumerate(keys[i:i + 2]):
            row.append(B(PAGES[k][0], f"help:{k}", style=colors[(i + j) % len(colors)]))
        rows.append(row)
    rows.append([B("⬅ Back", "help:home"), B("✖ Close", "help:close", style="danger")])
    return text, InlineKeyboardMarkup(rows)


def section_page(key):
    label, body = PAGES[key]
    return sc(body), InlineKeyboardMarkup([[B("⬅ Back", "help:main"), B("✖ Close", "help:close", style="danger")]])


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
    """/help [section] — jumps straight to that section instead of the main menu."""
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
    """Caches username -> id from every group message, so /promote @user, .ban @user
    etc. work even if that user has never DMed the bot. Also records group membership
    (for .waifu / .couple). Runs before commands, cheap: only writes to Mongo when
    something is new or changed."""
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
    for mod in (welcome, admin, afk, ban, sticker, fun, bot_filters):
        cmds += mod.COMMANDS
    await app.bot.set_my_commands(cmds)
    log.info("Started as @%s", app.bot_data["me"].username)


def main():
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

    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
