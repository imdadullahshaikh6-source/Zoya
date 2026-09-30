"""Moderation plugin: .ban / .mute / .warn (+ unban/unmute/unwarn), with an
undo button on every action. Fully cross-checked against the bot's real rights,
and every ban/mute/warn is stored in Mongo so it survives restarts."""
import os

from telegram import ChatPermissions, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler

import database as dbase
from common import (
    ADMIN, B, OWNER, T, dual_command, esc, get_member, mention, q, require_admin,
    resolve_target, rights_of, say, tag_missing,
)

WARN_LIMIT = int(os.getenv("WARN_LIMIT", "3"))

HELP_TXT = (
    "<b>✦ moderation — ban / mute / warn</b>\n\n"
    "/ban (or .ban) — reply / @username / id (+ optional reason)\n"
    "/kick (or .kick) — same usage, removes them but they can rejoin (no undo button)\n"
    "/mute (or .mute) — same usage, restricts sending messages\n"
    "/warn (or .warn) — after {n} warns the user is auto-muted\n"
    "/unban • /unmute • /unwarn — reverse any of the above\n\n"
    "every action re-checks that you have the <b>Ban Users</b> right and that i "
    "actually have it too — i tag you if I don't."
).replace("{n}", str(WARN_LIMIT))

COMMANDS = [
    ("ban", "Ban a user"), ("unban", "Unban a user"),
    ("kick", "Kick a user (they can rejoin)"),
    ("mute", "Mute a user"), ("unmute", "Unmute a user"),
    ("warn", "Warn a user"), ("unwarn", "Clear a user's warns"),
]

FULL_PERMS = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True, can_change_info=False, can_invite_users=True,
    can_pin_messages=False, can_manage_topics=False,
)
MUTE_PERMS = ChatPermissions(can_send_messages=False)


async def _target_or_complain(update, ctx, cmd):
    msg, chat = update.effective_message, update.effective_chat
    target, reason = await resolve_target(update, ctx)
    if not target:
        await say(ctx, chat.id, T("reply to a user, or use /{c} @username | user id [reason]", c=cmd), reply_to=msg.message_id)
        return None, None
    if target.id == ctx.bot.id:
        await say(ctx, chat.id, T("i can't do that to myself 🙂"), reply_to=msg.message_id)
        return None, None
    tm = await get_member(ctx, chat.id, target.id)
    if tm and tm.status == OWNER:
        await say(ctx, chat.id, T("that user is the group owner. nothing can be changed."), reply_to=msg.message_id)
        return None, None
    if tm and tm.status == ADMIN:
        await say(ctx, chat.id, T("{m} is an admin — demote first if you really want to do this.", m=mention(target)), reply_to=msg.message_id)
        return None, None
    return target, reason


async def _need_restrict(update, ctx):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        await say(ctx, chat.id, T("this only works inside groups."))
        return False
    if not await require_admin(update, ctx, "restrict_members"):
        return False
    bm = await get_member(ctx, chat.id, ctx.bot.id)
    if not rights_of(bm)["restrict_members"]:
        await tag_missing(ctx, chat.id, user, ["Ban Users"], msg.message_id)
        return False
    return True


def _undo_kb(label, data):
    return InlineKeyboardMarkup([[B(label, data, style="primary")]])


# ───────── BAN ─────────
async def ban_cmd(update, ctx):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not await _need_restrict(update, ctx):
        return
    target, reason = await _target_or_complain(update, ctx, "ban")
    if not target:
        return
    try:
        await ctx.bot.ban_chat_member(chat.id, target.id)
    except TelegramError as e:
        await say(ctx, chat.id, T("ban failed:") + f" {esc(e)}", reply_to=msg.message_id)
        return
    await dbase.mod_set(chat.id, target.id, "ban", user.id, reason)
    text = T(
        "<b>🚫 BAN EVENT</b>\n\nuser: {u}\nby admin: {a}\nreason: {r}",
        u=mention(target), a=mention(user), r=esc(reason) if reason else "no reason given",
    )
    await ctx.bot.send_message(chat.id, q(text), reply_markup=_undo_kb("♻ Unban", f"mod:unban:{target.id}"))


async def unban_cmd(update, ctx):
    msg, chat = update.effective_message, update.effective_chat
    if not await _need_restrict(update, ctx):
        return
    target, _ = await _target_or_complain(update, ctx, "unban")
    if not target:
        return
    try:
        await ctx.bot.unban_chat_member(chat.id, target.id, only_if_banned=True)
    except TelegramError as e:
        await say(ctx, chat.id, T("unban failed:") + f" {esc(e)}", reply_to=msg.message_id)
        return
    await dbase.mod_clear(chat.id, target.id)
    await say(ctx, chat.id, T("✅ {m} has been unbanned.", m=mention(target)), reply_to=msg.message_id)


# ───────── KICK (ban immediately followed by unban — no permanent ban, no button) ─────────
async def kick_cmd(update, ctx):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not await _need_restrict(update, ctx):
        return
    target, reason = await _target_or_complain(update, ctx, "kick")
    if not target:
        return
    try:
        await ctx.bot.ban_chat_member(chat.id, target.id)
        await ctx.bot.unban_chat_member(chat.id, target.id, only_if_banned=True)
    except TelegramError as e:
        await say(ctx, chat.id, T("kick failed:") + f" {esc(e)}", reply_to=msg.message_id)
        return
    text = T(
        "<b>👢 KICKED</b>\n\nuser: {u}\nby admin: {a}\nreason: {r}",
        u=mention(target), a=mention(user), r=esc(reason) if reason else "no reason given",
    )
    await ctx.bot.send_message(chat.id, q(text))


# ───────── MUTE ─────────
async def mute_cmd(update, ctx):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not await _need_restrict(update, ctx):
        return
    target, reason = await _target_or_complain(update, ctx, "mute")
    if not target:
        return
    try:
        await ctx.bot.restrict_chat_member(chat.id, target.id, permissions=MUTE_PERMS)
    except TelegramError as e:
        await say(ctx, chat.id, T("mute failed:") + f" {esc(e)}", reply_to=msg.message_id)
        return
    await dbase.mod_set(chat.id, target.id, "mute", user.id, reason)
    text = T(
        "<b>🔇 MUTE EVENT</b>\n\nuser: {u}\nby admin: {a}\nreason: {r}",
        u=mention(target), a=mention(user), r=esc(reason) if reason else "no reason given",
    )
    await ctx.bot.send_message(chat.id, q(text), reply_markup=_undo_kb("🔊 Unmute", f"mod:unmute:{target.id}"))


async def unmute_cmd(update, ctx):
    msg, chat = update.effective_message, update.effective_chat
    if not await _need_restrict(update, ctx):
        return
    target, _ = await _target_or_complain(update, ctx, "unmute")
    if not target:
        return
    try:
        await ctx.bot.restrict_chat_member(chat.id, target.id, permissions=FULL_PERMS)
    except TelegramError as e:
        await say(ctx, chat.id, T("unmute failed:") + f" {esc(e)}", reply_to=msg.message_id)
        return
    await dbase.mod_clear(chat.id, target.id)
    await say(ctx, chat.id, T("✅ {m} has been unmuted.", m=mention(target)), reply_to=msg.message_id)


# ───────── WARN ─────────
async def warn_cmd(update, ctx):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not await require_admin(update, ctx, "restrict_members"):
        return
    target, reason = await _target_or_complain(update, ctx, "warn")
    if not target:
        return
    count = await dbase.warn_add(chat.id, target.id, user.id, reason)
    text = T(
        "<b>⚠ WARN EVENT</b>\n\nuser: {u}\nby admin: {a}\nreason: {r}\nwarns: {c}/{n}",
        u=mention(target), a=mention(user), r=esc(reason) if reason else "no reason given",
        c=count, n=WARN_LIMIT,
    )
    kb = _undo_kb("♻ Remove Warns", f"mod:unwarn:{target.id}")
    if count >= WARN_LIMIT:
        bm = await get_member(ctx, chat.id, ctx.bot.id)
        if rights_of(bm)["restrict_members"]:
            try:
                await ctx.bot.restrict_chat_member(chat.id, target.id, permissions=MUTE_PERMS)
                await dbase.mod_set(chat.id, target.id, "mute", user.id, "reached warn limit")
                text += "\n\n" + T("🔇 warn limit reached — user has been muted.")
            except TelegramError as e:
                text += "\n\n" + T("⚠ warn limit reached but mute failed:") + f" {esc(e)}"
        else:
            text += "\n\n" + T("⚠ warn limit reached, but i don't have the Ban Users power to mute.")
    await ctx.bot.send_message(chat.id, q(text), reply_markup=kb)


async def unwarn_cmd(update, ctx):
    msg, chat = update.effective_message, update.effective_chat
    if not await require_admin(update, ctx, "restrict_members"):
        return
    target, _ = await _target_or_complain(update, ctx, "unwarn")
    if not target:
        return
    await dbase.warn_clear(chat.id, target.id)
    await say(ctx, chat.id, T("✅ warns cleared for {m}.", m=mention(target)), reply_to=msg.message_id)


# ───────── UNDO BUTTONS ─────────
async def mod_cb(update, ctx):
    qy = update.callback_query
    chat = update.effective_chat
    action, uid = qy.data.split(":")[1], int(qy.data.split(":")[2])
    m = await get_member(ctx, chat.id, qy.from_user.id)
    if not m or m.status not in (OWNER, ADMIN) or (m.status == ADMIN and not m.can_restrict_members):
        await qy.answer("You don't have the Ban Users right.", show_alert=True)
        return
    bm = await get_member(ctx, chat.id, ctx.bot.id)
    if not rights_of(bm)["restrict_members"]:
        await qy.answer("I don't have the Ban Users power.", show_alert=True)
        return
    target_mention = f'<a href="tg://user?id={uid}">this user</a>'
    try:
        if action == "unban":
            await ctx.bot.unban_chat_member(chat.id, uid, only_if_banned=True)
            await dbase.mod_clear(chat.id, uid)
            new_text = T("<b>✅ unbanned</b>\n\n{m} was unbanned by {a}.", m=target_mention, a=mention(qy.from_user))
        elif action == "unmute":
            await ctx.bot.restrict_chat_member(chat.id, uid, permissions=FULL_PERMS)
            await dbase.mod_clear(chat.id, uid)
            new_text = T("<b>✅ unmuted</b>\n\n{m} was unmuted by {a}.", m=target_mention, a=mention(qy.from_user))
        elif action == "unwarn":
            await dbase.warn_clear(chat.id, uid)
            new_text = T("<b>✅ warns cleared</b>\n\n{m}'s warns were cleared by {a}.", m=target_mention, a=mention(qy.from_user))
        else:
            await qy.answer()
            return
    except TelegramError as e:
        await qy.answer("Failed, see message.", show_alert=False)
        await qy.message.reply_text(q(T("action failed:") + f" {esc(e)}"))
        return
    await qy.edit_message_text(q(new_text))
    await qy.answer("Done ✅")


def register(app):
    dual_command(app, "ban", ban_cmd)
    dual_command(app, "unban", unban_cmd)
    dual_command(app, "kick", kick_cmd)
    dual_command(app, "mute", mute_cmd)
    dual_command(app, "unmute", unmute_cmd)
    dual_command(app, "warn", warn_cmd)
    dual_command(app, "unwarn", unwarn_cmd)
    app.add_handler(CallbackQueryHandler(mod_cb, pattern=r"^mod:"))
    
