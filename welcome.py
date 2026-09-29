"""
ZOYA Welcome Plugin
Features:
- Rose-style welcome buttons
- Text, photo, video, GIF, sticker, document welcomes
- Direct member add detection
- Join request detection
- Approved join request detection
- Clean welcome / clean service
- Group rules
- Admin permission checks
"""

import re

from telegram import ReplyParameters
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    MessageHandler,
    filters,
    ChatJoinRequestHandler,
    ChatMemberHandler,
)

import database as dbase

from common import (
    B, T, dual_command, esc, get_member, mention, parse_buttons,
    perm_report, q, require_admin, rights_of, say, tag_missing,
)

cfg_get = dbase.cfg_get
cfg_set = dbase.cfg_set


HELP_TXT = (
    "<b>✦ greetings — welcome system</b>\n\n"
    "/setwelcome (or .setwelcome) — reply to any message or type text after it\n"
    "/welcome — status + preview • /welcome on|off\n"
    "/resetwelcome — back to default\n"
    "/cleanwelcome on|off — delete old welcome when a new member joins\n"
    "/cleanservice on|off — delete join service messages\n"
    "/setrules • /rules — group rules\n\n"
    "<b>fillings:</b> {first} {last} {fullname} {username} "
    "{mention} {id} {chatname} {count}\n\n"
    "<b>buttons:</b>\n"
    "[Label](buttonurl://https://link.com)\n"
    "[A](buttonurl://https://a.com:same) "
    "[B](buttonurl://https://b.com:same)\n\n"
    "add :primary / :success / :danger for color\n"
    "only admins with change info can set these."
)


COMMANDS = [
    ("setwelcome", "Set welcome message"),
    ("welcome", "Welcome status / on / off"),
    ("resetwelcome", "Reset welcome"),
    ("cleanwelcome", "Delete old welcome messages"),
    ("cleanservice", "Delete join service messages"),
    ("setrules", "Set group rules"),
    ("rules", "Show group rules"),
]


DEFAULT_WELCOME = {
    "type": "text",
    "file_id": None,
    "text": (
        "<blockquote>✦ <b>Welcome to {chatname}</b>\n\n"
        "➤ Dear - {first}\n"
        "➤ ID:- <code>{id}</code>\n"
        "➤ Username:- {username}</blockquote>\n"
        "<blockquote>Hey welcome, we're so happy to have you here! ✨\n"
        "Make yourself comfy, join the chat, meet new people &amp; "
        "enjoy the vibes. Check /rules if we have any.</blockquote>"
    ),
}


# --------------------------------------------------
# MEMBER DETECTION HELPERS
# --------------------------------------------------

def is_member_status(status):
    """
    Returns True when Telegram reports that the user
    is currently a member of the group.
    """
    return status in (
        "member",
        "administrator",
        "creator",
    )


def already_processed(ctx, chat_id, user_id):
    """
    Prevent duplicate welcomes when Telegram sends both
    NEW_CHAT_MEMBERS and CHAT_MEMBER updates.
    """
    seen = ctx.application.bot_data.setdefault(
        "welcome_processed_members", set()
    )

    key = (chat_id, user_id)

    if key in seen:
        return True

    # Prevent unlimited growth in bot memory.
    if len(seen) >= 5000:
        seen.clear()

    seen.add(key)
    return False


def media_of(m):
    if m.photo:
        return "photo", m.photo[-1].file_id

    if m.video:
        return "video", m.video.file_id

    if m.animation:
        return "animation", m.animation.file_id

    if m.sticker:
        return "sticker", m.sticker.file_id

    if m.document:
        return "document", m.document.file_id

    return "text", None


def extract(msg, cmd: str):
    src = msg.reply_to_message

    if src:
        kind, fid = media_of(src)
        text = src.text_html if src.text else (
            src.caption_html or ""
        )
    else:
        kind, fid = media_of(msg)

        raw = msg.text_html if msg.text else (
            msg.caption_html or ""
        )

        text = re.sub(
            rf"^[/.]{cmd}(@\w+)?\s*",
            "",
            raw or "",
            flags=re.I,
        )

    text = (text or "").strip()

    if kind == "text" and not text:
        return None

    return {
        "type": kind,
        "file_id": fid,
        "text": text,
    }


def render(tpl: str, user, chat, count, botuser) -> str:
    rep = {
        "{first}": esc(user.first_name or ""),
        "{last}": esc(user.last_name or ""),
        "{fullname}": esc(user.full_name or ""),
        "{username}": (
            f"@{esc(user.username)}"
            if user.username else mention(user)
        ),
        "{mention}": mention(user),
        "{id}": str(user.id),
        "{chatname}": esc(chat.title or ""),
        "{count}": str(count),
        "{chatid}": str(chat.id),
        "{botuser}": botuser or "",
    }

    for k, v in rep.items():
        tpl = tpl.replace(k, v)

    return tpl


# --------------------------------------------------
# SEND WELCOME
# --------------------------------------------------

async def send_welcome(ctx, chat, user, cfg):
    w = cfg.get("welcome") or DEFAULT_WELCOME

    try:
        count = await ctx.bot.get_chat_member_count(chat.id)
    except TelegramError:
        count = "?"

    me = ctx.application.bot_data.get("me")

    raw = render(
        w.get("text") or "",
        user,
        chat,
        count,
        me.username if me else "",
    )

    text, kb = parse_buttons(raw)

    kind = w.get("type", "text")
    fid = w.get("file_id")

    async def _send(parse_mode):
        cap = text or None

        if kind == "photo":
            return await ctx.bot.send_photo(
                chat.id,
                fid,
                caption=cap,
                parse_mode=parse_mode,
                reply_markup=kb,
            )

        if kind == "video":
            return await ctx.bot.send_video(
                chat.id,
                fid,
                caption=cap,
                parse_mode=parse_mode,
                reply_markup=kb,
            )

        if kind == "animation":
            return await ctx.bot.send_animation(
                chat.id,
                fid,
                caption=cap,
                parse_mode=parse_mode,
                reply_markup=kb,
            )

        if kind == "document":
            return await ctx.bot.send_document(
                chat.id,
                fid,
                caption=cap,
                parse_mode=parse_mode,
                reply_markup=kb,
            )

        if kind == "sticker":
            s = await ctx.bot.send_sticker(
                chat.id,
                fid,
            )

            if text:
                await ctx.bot.send_message(
                    chat.id,
                    text,
                    parse_mode=parse_mode,
                    reply_markup=kb,
                )

            return s

        return await ctx.bot.send_message(
            chat.id,
            text or "Welcome!",
            parse_mode=parse_mode,
            reply_markup=kb,
        )

    try:
        return await _send(ParseMode.HTML)

    except BadRequest as e:
        if (
            "parse" in str(e).lower()
            or "entities" in str(e).lower()
        ):
            return await _send(None)

        raise


# --------------------------------------------------
# SET WELCOME
# --------------------------------------------------

async def setwelcome_cmd(update, ctx):
    msg = update.message
    chat = update.effective_chat

    if not msg:
        return

    if not await require_admin(update, ctx, "change_info"):
        return

    data = extract(msg, "setwelcome")

    if not data:
        await say(
            ctx,
            chat.id,
            T(
                "reply to a message with /setwelcome, "
                "or write text after it.\n"
                "photo / video / gif / sticker also work "
                "(send media with caption /setwelcome).\n"
                "fillings: {f}\n"
                "buttons: [Label](buttonurl://link:same:danger)",
                f=(
                    "{first} {last} {fullname} {username} "
                    "{mention} {id} {chatname} {count}"
                ),
            ),
            reply_to=msg.message_id,
        )
        return

    await dbase.save_welcome(chat.id, data)

    cfg = await cfg_get(chat.id)

    try:
        await send_welcome(
            ctx,
            chat,
            msg.from_user,
            cfg,
        )

        prev = T(
            "✅ welcome saved (type: {t}). preview sent above.",
            t=data["type"],
        )

    except TelegramError as e:
        prev = (
            T("⚠ saved, but preview failed:")
            + f" {esc(e)}"
        )

    bm = await get_member(
        ctx,
        chat.id,
        ctx.bot.id,
    )

    extra = ""

    if not rights_of(bm)["delete_messages"]:
        extra = "\n\n" + T(
            "{m}, i don't have the {l} power. "
            "/cleanwelcome and /cleanservice will not work "
            "until you give it.",
            m=mention(msg.from_user),
            l="<b>Delete Messages</b>",
        )

    await say(
        ctx,
        chat.id,
        prev + "\n\n" + await perm_report(ctx, chat) + extra,
        reply_to=msg.message_id,
    )


# --------------------------------------------------
# WELCOME STATUS
# --------------------------------------------------

async def welcome_cmd(update, ctx):
    msg = update.message
    chat = update.effective_chat

    if not msg:
        return

    if not await require_admin(update, ctx, "change_info"):
        return

    cfg = await cfg_get(chat.id)

    arg = ctx.args[0].lower() if ctx.args else ""

    if arg in ("on", "off", "yes", "no"):
        val = arg in ("on", "yes")

        await cfg_set(
            chat.id,
            welcome_on=val,
        )

        await say(
            ctx,
            chat.id,
            T(
                "welcome messages are now {s}.",
                s=(
                    "<b>ON ✅</b>"
                    if val else "<b>OFF ❌</b>"
                ),
            ),
            reply_to=msg.message_id,
        )
        return

    w = cfg.get("welcome") or DEFAULT_WELCOME

    status = T(
        "<b>welcome settings</b>\n"
        "welcome: {a}\n"
        "clean welcome: {b}\n"
        "clean service: {c}\n"
        "message type: {t}",
        a=(
            "ON ✅"
            if cfg.get("welcome_on", True)
            else "OFF ❌"
        ),
        b=(
            "ON ✅"
            if cfg.get("clean_welcome")
            else "OFF ❌"
        ),
        c=(
            "ON ✅"
            if cfg.get("clean_service")
            else "OFF ❌"
        ),
        t=w.get("type", "text"),
    )

    await say(
        ctx,
        chat.id,
        status + "\n\n" + await perm_report(ctx, chat),
        reply_to=msg.message_id,
    )

    try:
        await send_welcome(
            ctx,
            chat,
            msg.from_user,
            cfg,
        )

    except TelegramError as e:
        await say(
            ctx,
            chat.id,
            T("preview failed:") + f" {esc(e)}",
        )


# --------------------------------------------------
# RESET WELCOME
# --------------------------------------------------

async def resetwelcome_cmd(update, ctx):
    msg = update.message
    chat = update.effective_chat

    if not msg:
        return

    if not await require_admin(update, ctx, "change_info"):
        return

    await dbase.reset_welcome(chat.id)

    await say(
        ctx,
        chat.id,
        T("welcome reset to default ✅"),
        reply_to=msg.message_id,
    )


# --------------------------------------------------
# CLEAN WELCOME / CLEAN SERVICE
# --------------------------------------------------

async def _toggle(update, ctx, key, label, cmd):
    msg = update.message
    chat = update.effective_chat

    if not msg:
        return

    if not await require_admin(update, ctx, "change_info"):
        return

    arg = ctx.args[0].lower() if ctx.args else ""

    cfg = await cfg_get(chat.id)

    if arg not in ("on", "off", "yes", "no"):
        cur = (
            "ON ✅"
            if cfg.get(key)
            else "OFF ❌"
        )

        await say(
            ctx,
            chat.id,
            T(
                "{l} is currently {c}.\n"
                "usage: /{cmd} on|off",
                l=label,
                c=cur,
                cmd=cmd,
            ),
            reply_to=msg.message_id,
        )
        return

    val = arg in ("on", "yes")

    if val:
        bm = await get_member(
            ctx,
            chat.id,
            ctx.bot.id,
        )

        if not rights_of(bm)["delete_messages"]:
            await tag_missing(
                ctx,
                chat.id,
                msg.from_user,
                ["Delete Messages"],
                msg.message_id,
            )
            return

    await cfg_set(
        chat.id,
        **{key: val},
    )

    await say(
        ctx,
        chat.id,
        T(
            "{l} is now {s}.",
            l=label,
            s=(
                "<b>ON ✅</b>"
                if val else "<b>OFF ❌</b>"
            ),
        ),
        reply_to=msg.message_id,
    )


async def cleanwelcome_cmd(update, ctx):
    await _toggle(
        update,
        ctx,
        "clean_welcome",
        "clean welcome",
        "cleanwelcome",
    )


async def cleanservice_cmd(update, ctx):
    await _toggle(
        update,
        ctx,
        "clean_service",
        "clean service",
        "cleanservice",
    )


# --------------------------------------------------
# RULES
# --------------------------------------------------

async def setrules_cmd(update, ctx):
    msg = update.message
    chat = update.effective_chat

    if not msg:
        return

    if not await require_admin(update, ctx, "change_info"):
        return

    data = extract(msg, "setrules")

    if not data or not data["text"]:
        await say(
            ctx,
            chat.id,
            T(
                "reply to a text message with /setrules "
                "or write the rules after it."
            ),
            reply_to=msg.message_id,
        )
        return

    await cfg_set(
        chat.id,
        rules=data["text"],
    )

    await say(
        ctx,
        chat.id,
        T(
            "rules saved ✅ members can use /rules"
        ),
        reply_to=msg.message_id,
    )


async def rules_cmd(update, ctx):
    msg = update.message
    chat = update.effective_chat

    if not msg:
        return

    if chat.type == ChatType.PRIVATE:
        await say(
            ctx,
            chat.id,
            T("use this command in a group."),
        )
        return

    cfg = await cfg_get(chat.id)

    if not cfg.get("rules"):
        await say(
            ctx,
            chat.id,
            T(
                "no rules set yet. admins can use /setrules."
            ),
            reply_to=msg.message_id,
        )
        return

    text, kb = parse_buttons(
        cfg["rules"]
    )

    await ctx.bot.send_message(
        chat.id,
        q(text),
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
        reply_parameters=ReplyParameters(
            message_id=msg.message_id,
            allow_sending_without_reply=True,
        ),
    )


# --------------------------------------------------
# BOT PERMISSIONS
# --------------------------------------------------

async def botperms_cmd(update, ctx):
    msg = update.message
    chat = update.effective_chat

    if not msg:
        return

    if not await require_admin(update, ctx):
        return

    await say(
        ctx,
        chat.id,
        await perm_report(ctx, chat),
        reply_to=msg.message_id,
    )


# --------------------------------------------------
# COMMON WELCOME PROCESSOR
# --------------------------------------------------

async def process_new_member(ctx, chat, user):
    """
    Shared function for direct joins and approved requests.
    Checks settings, ignores bots and prevents duplicate welcomes.
    """

    from common import log

    if user.is_bot:
        return

    cfg = await cfg_get(chat.id)

    if not cfg.get("welcome_on", True):
        log.info(
            "Welcome disabled in chat %s",
            chat.id,
        )
        return

    if already_processed(
        ctx,
        chat.id,
        user.id,
    ):
        log.info(
            "Duplicate welcome ignored: chat=%s user=%s",
            chat.id,
            user.id,
        )
        return

    if (
        cfg.get("clean_welcome")
        and cfg.get("last_welcome")
    ):
        try:
            await ctx.bot.delete_message(
                chat.id,
                cfg["last_welcome"],
            )

        except TelegramError:
            pass

    try:
        sent = await send_welcome(
            ctx,
            chat,
            user,
            cfg,
        )

        await cfg_set(
            chat.id,
            last_welcome=sent.message_id,
        )

        log.info(
            "Welcome sent successfully: chat=%s user=%s",
            chat.id,
            user.id,
        )

    except TelegramError as e:
        log.warning(
            "Welcome failed in chat %s for user %s: %s",
            chat.id,
            user.id,
            e,
        )


# --------------------------------------------------
# DIRECT ADD / NEW MEMBERS SERVICE UPDATE
# --------------------------------------------------

async def on_join(update, ctx):
    msg = update.message
    chat = update.effective_chat

    if not msg or not msg.new_chat_members:
        return

    cfg = await cfg_get(chat.id)

    # Delete Telegram's "user joined" service message
    # if cleanservice is enabled.
    if cfg.get("clean_service"):
        try:
            await msg.delete()

        except TelegramError:
            pass

    for user in msg.new_chat_members:

        # Bot itself added to the group
        if user.id == ctx.bot.id:
            from common import log

            log.info(
                "ZOYA added to chat %s",
                chat.id,
            )

            await say(
                ctx,
                chat.id,
                T(
                    "thanks for adding me! ✨ "
                    "make me admin so every feature works."
                )
                + "\n\n"
                + await perm_report(ctx, chat),
            )
            continue

        # Ignore bots and process human members
        if not user.is_bot:
            await process_new_member(
                ctx,
                chat,
                user,
            )


# --------------------------------------------------
# JOIN REQUEST DETECTION
# --------------------------------------------------

async def on_join_request(update, ctx):
    """
    Detects pending join requests.

    A pending request is NOT a group membership.
    Therefore, do not send the normal group welcome yet.
    """

    request = update.chat_join_request

    if not request:
        return

    from common import log

    chat = request.chat
    user = request.from_user

    log.info(
        "JOIN REQUEST RECEIVED | chat=%s | user=%s",
        chat.id,
        user.id,
    )

    # Welcome will be sent after Telegram reports
    # that this user actually becomes a member.


# --------------------------------------------------
# APPROVED REQUEST / CHAT MEMBER UPDATE
# --------------------------------------------------

async def on_chat_member(update, ctx):
    """
    Detects membership status changes.

    This is important because a user approved from a
    join request may not produce the usual new_chat_members
    service message in every update delivery scenario.
    """

    member_update = update.chat_member

    if not member_update:
        return

    chat = member_update.chat
    old_member = member_update.old_chat_member
    new_member = member_update.new_chat_member

    old_status = old_member.status
    new_status = new_member.status

    was_member = is_member_status(
        old_status
    )

    is_now_member = is_member_status(
        new_status
    )

    # Only process transitions from non-member
    # to member. Ignore promotions and unrelated updates.
    if was_member or not is_now_member:
        return

    user = new_member.user

    from common import log

    log.info(
        "NEW MEMBER DETECTED BY CHAT_MEMBER | "
        "chat=%s | user=%s | old=%s | new=%s",
    chat.id,
        user.id,
        old_status,
        new_status,
    )

    await process_new_member(
        ctx,
        chat,
        user,
    )


# --------------------------------------------------
# REGISTER HANDLERS
# --------------------------------------------------

def register(app):

    grp = filters.ChatType.GROUPS

    # Welcome commands
    dual_command(
        app,
        "setwelcome",
        setwelcome_cmd,
    )

# --------------------------------------------------
# REGISTER HANDLERS
# --------------------------------------------------

def register(app):

    grp = filters.ChatType.GROUPS

    # Welcome commands
    dual_command(
        app,
        "setwelcome",
        setwelcome_cmd,
    )

    # Support media captions such as:
    # photo + caption /setwelcome
    app.add_handler(
        MessageHandler(
            filters.CaptionRegex(
                re.compile(
                    r"^[/.]setwelcome(@\w+)?(\s|$)",
                    re.I,
                )
            ) & grp,
            setwelcome_cmd,
        )
    )
    
    dual_command(
        app,
        "welcome",
        welcome_cmd,
    )

    dual_command(
        app,
        "resetwelcome",
        resetwelcome_cmd,
    )

    dual_command(
        app,
        "cleanwelcome",
        cleanwelcome_cmd,
    )

    dual_command(
        app,
        "cleanservice",
        cleanservice_cmd,
    )

    # Rules
    dual_command(
        app,
        "setrules",
        setrules_cmd,
    )

    dual_command(
        app,
        "rules",
        rules_cmd,
        group_only=False,
    )

    # Permissions
    dual_command(
        app,
        "botperms",
        botperms_cmd,
    )

    # 1. Direct adds / Telegram new member service messages
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            on_join,
        )
    )

    # 2. Join requests
    app.add_handler(
        ChatJoinRequestHandler(
            on_join_request
        )
    )
    
    # 3. Approved requests and other membership changes
    app.add_handler(
        ChatMemberHandler(
            on_chat_member,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )
