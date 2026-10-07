"""Admin plugin: /promote and /demote with a live power-selection panel."""
from collections import OrderedDict

from telegram import InlineKeyboardMarkup, ReplyParameters
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import CallbackQueryHandler

import logchannel
from common import (
    ADMIN, B, OWNER, RIGHTS, RL, T, dual_command, esc, explain, get_member,
    mention, q, require_admin, resolve_target, rights_of, say, tag_missing,
)

# ⚠️ APNI TELEGRAM ID YAHAN DAALEIN
BOT_OWNER_ID = 8373739674

# Telegram's official Anonymous Admin Bot ID
ANON_ADMIN_ID = 1087968824

HELP_TXT = (
    "<b>✦ admin — promote &amp; demote</b>\n\n"
    "/promote (or .promote) — reply / @username / id (+ optional custom title). "
    "a panel opens: tap ✅ / ❌ to choose powers, ⚡ full power, then ✅ promote.\n"
    "/demote (or .demote) — removes all admin powers after confirmation\n\n"
    "every action is verified: you need add admins, and i must have it too — plus every power i give. "
    "if i lack something i tag you and say so. 🔒 buttons = powers i don't have."
)

COMMANDS = [("promote", "Promote a user"), ("demote", "Demote an admin")]

PANELS: "OrderedDict[str, dict]" = OrderedDict()
ANON_PENDING = {}  # Anonymous verification pending actions

# How many permission buttons per page
PER_PAGE = 4


# ───────────────────── FANCY FONT CONVERTER ─────────────────────
_FANCY_MAP = {}
for _n, _f in zip("abcdefghijklmnopqrstuvwxyz",
                  "𝙖𝙗𝙘𝙙𝙚𝙛𝙜𝙝𝙞𝙟𝙠𝙡𝙢𝙣𝙤𝙥𝙦𝙧𝙨𝙩𝙪𝙫𝙬𝙭𝙮𝙯"):
    _FANCY_MAP[_n] = _f
for _n, _f in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                  "𝘼𝘽𝘾𝘿𝙀𝙁𝙂𝙃𝙄𝙅𝙆𝙇𝙈𝙉𝙊𝙋𝙌𝙍𝙎𝙏𝙐𝙑𝙒𝙓𝙔𝙕"):
    _FANCY_MAP[_n] = _f


def _fancy(s: str) -> str:
    """Convert string to bold-italic sans-serif unicode (𝙘𝙝𝙖𝙣𝙜𝙚 𝙞𝙣𝙛𝙤 style)."""
    if not s:
        return ""
    return "".join(_FANCY_MAP.get(c, c) for c in s)


def _put(key, st):
    PANELS[key] = st
    while len(PANELS) > 300:
        PANELS.popitem(last=False)


def add_me_url(username: str) -> str:
    rights = (
        "change_info+delete_messages+restrict_members+invite_users+pin_messages"
        "+manage_video_chats+manage_topics+promote_members+manage_chat"
    )
    return f"https://t.me/{username}?startgroup=true&admin={rights}"


async def _get_real_group_owner_id(ctx, chat_id: int):
    try:
        admins = await ctx.bot.get_chat_administrators(chat_id)
        for admin in admins:
            if admin.status == ChatMemberStatus.OWNER:
                return admin.user.id
    except TelegramError as e:
        print(f"[admin] failed to get owner id: {e}")
    return None


async def _precheck(update, ctx, mode: str):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        me = ctx.application.bot_data["me"]
        kb = InlineKeyboardMarkup([[B("➕ Add Me To Group", url=add_me_url(me.username), style="success")]])
        await say(ctx, chat.id, T("/promote and /demote work inside groups. add me to your group and make me admin."), kb=kb)
        return None

    # 🔹 Anonymous Admin Detection
    if user.id == ANON_ADMIN_ID:
        target, title = await resolve_target(update, ctx)
        if not target:
            await say(ctx, chat.id, T("reply to a user, or use /{c} @username | user id", c=mode), reply_to=msg.message_id)
            return None

        action_id = f"anon_{mode}_{chat.id}_{msg.message_id}"
        ANON_PENDING[action_id] = {
            "mode": mode,
            "chat_id": chat.id,
            "target_id": target.id,
            "target_name": target.first_name,
            "title": title[:16] if title else "",
            "invoker_msg_id": msg.message_id
        }

        kb = InlineKeyboardMarkup([[B("𝙥𝙧𝙤𝙫𝙚 𝙊𝙬𝙣𝙚𝙧/𝙖𝙙𝙢𝙞𝙣", action_id, style="success")]])
        await ctx.bot.send_message(
            chat.id,
            q(T("<b>⚠️ Anonymous Admin detected.</b>\n"
                "Only the real group owner can approve this. "
                "Please tap the button below to verify.")),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=msg.message_id,
            reply_markup=kb,
        )
        return None

    if msg.sender_chat:
        await say(ctx, chat.id, T("you are anonymous. turn off 'remain anonymous' in your admin rights and try again."), reply_to=msg.message_id)
        return None
    if not await require_admin(update, ctx, "promote_members"):
        return None
    target, title = await resolve_target(update, ctx)
    if not target:
        await say(ctx, chat.id, T("reply to a user, or use /{c} @username | user id", c=mode), reply_to=msg.message_id)
        return None
    if target.id == ctx.bot.id:
        await say(ctx, chat.id, T("i can't do that to myself 🙂"), reply_to=msg.message_id)
        return None
    bm = await get_member(ctx, chat.id, ctx.bot.id)
    br = rights_of(bm)
    if not br["promote_members"]:
        if not bm or bm.status not in (ADMIN, OWNER):
            await say(ctx, chat.id, T("{m}, i am not an admin here. make me admin with the {l} power first.", m=mention(user), l="<b>Add New Admins</b>"), reply_to=msg.message_id)
        else:
            await tag_missing(ctx, chat.id, user, ["Add New Admins"], msg.message_id)
        return None
    tm = await get_member(ctx, chat.id, target.id)
    if tm is None or tm.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        await say(ctx, chat.id, T("that user is not in this group."), reply_to=msg.message_id)
        return None
    if tm.status == OWNER:
        await say(ctx, chat.id, T("that user is the group owner. nothing can be changed."), reply_to=msg.message_id)
        return None
    if tm.status == ADMIN and not tm.can_be_edited:
        await say(ctx, chat.id, T("{m} is an admin promoted by someone else, so i can't edit their powers. only that admin or the owner can.", m=mention(target)), reply_to=msg.message_id)
        return None
    if mode == "demote" and tm.status != ADMIN:
        await say(ctx, chat.id, T("{m} is not an admin.", m=mention(target)), reply_to=msg.message_id)
        return None
    return dict(chat=chat, user=user, target=target, title=title[:16], tm=tm, br=br, msg=msg)


# ───────────────────── PANEL BUILDERS ─────────────────────

def _all_items(st):
    """Return list of (key, label) including anonymous."""
    items = list(st["rights_list"])
    items.append(("anonymous", "Anonymous"))
    return items


def _panel_text(st) -> str:
    items = _all_items(st)
    total_pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
    page = st.get("page", 0)
    selected = sum(1 for k, _ in items if st["sel"].get(k))

    lines = [
        T("<b>✦ 𝙋𝙧𝙤𝙢𝙤𝙩𝙚 𝙎𝙚𝙩𝙪𝙥</b>"),
        "",
        "👤 " + T("User: ") + st["tgt_m"],
    ]
    if st["title"]:
        lines.append("🏷 " + T("Title: ") + f"<b>{esc(st['title'])}</b>")
    lines.append(f"📄 {_fancy('Page')}: <b>{page + 1}/{total_pages}</b>")
    lines.append(f"✅ {_fancy('Selected Rights')}: <b>{selected}</b>")
    if st["missing"]:
        lines += ["", T("{m}, i don't have: {l}. those buttons are locked 🔒",
                        m=st["inv_m"],
                        l="<b>" + esc(", ".join(st["missing"])) + "</b>")]
    return "\n".join(lines)


def _panel_kb(st):
    items = _all_items(st)
    total_pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
    page = st.get("page", 0)
    start = page * PER_PAGE
    end = start + PER_PAGE
    page_items = items[start:end]

    btns = []
    for k, label in page_items:
        on = bool(st["sel"].get(k))
        mark = "🟢" if on else "🔴"
        btns.append(B(
            f"{mark} {_fancy(label)}",
            f"pr:t:{k}",
            style="success" if on else "danger",
        ))
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]

    # Nav row: Back / Next (blue)
    nav = []
    if page > 0:
        nav.append(B("⬅️ " + _fancy("Back"), "pr:prev", style="primary"))
    else:
        nav.append(B("⬅️ " + _fancy("Back"), "pr:noop", style="primary"))
    if page < total_pages - 1:
        nav.append(B(_fancy("Next") + " ➡️", "pr:next", style="primary"))
    else:
        nav.append(B(_fancy("Next") + " ➡️", "pr:noop", style="primary"))
    rows.append(nav)

    # Full power + Clear all
    rows.append([
        B("⚡ " + _fancy("Full Power"), "pr:full", style="primary"),
        B("🧹 " + _fancy("Clear All"), "pr:clear", style="danger"),
    ])
    # Confirm + Cancel
    rows.append([
        B("✨ ✅ " + _fancy("Confirm"), "pr:go", style="success"),
        B("✖ " + _fancy("Cancel"), "pr:x", style="danger"),
    ])
    return InlineKeyboardMarkup(rows)


async def promote_cmd(update, ctx):
    if not update.message:
        return
    p = await _precheck(update, ctx, "promote")
    if not p:
        return
    chat, user, target, tm, br = p["chat"], p["user"], p["target"], p["tm"], p["br"]
    rights_list = [(k, l) for k, l in RIGHTS if k != "manage_topics" or chat.is_forum]
    if tm.status == ADMIN:
        cur = rights_of(tm)
        sel = {k: cur[k] and br[k] for k, _ in rights_list}
        sel["anonymous"] = bool(tm.is_anonymous)
    else:
        sel = {k: (k in ("delete_messages", "invite_users", "pin_messages")) and br[k] for k, _ in rights_list}
    st = dict(
        mode="promote", chat_id=chat.id, invoker=user.id, target=target.id,
        tgt_m=mention(target), inv_m=mention(user), title=p["title"],
        rights_list=rights_list, bot=br, sel=sel, forum=bool(chat.is_forum),
        missing=[l for k, l in rights_list if not br[k]],
        page=0,
    )
    sent = await ctx.bot.send_message(
        chat.id, q(_panel_text(st)), reply_markup=_panel_kb(st),
        reply_parameters=ReplyParameters(message_id=p["msg"].message_id, allow_sending_without_reply=True),
    )
    _put(f"{chat.id}:{sent.message_id}", st)


async def demote_cmd(update, ctx):
    if not update.message:
        return
    p = await _precheck(update, ctx, "demote")
    if not p:
        return
    chat, user, target = p["chat"], p["user"], p["target"]
    st = dict(mode="demote", chat_id=chat.id, invoker=user.id, target=target.id,
              tgt_m=mention(target), inv_m=mention(user), forum=bool(chat.is_forum))
    kb = InlineKeyboardMarkup([[
        B("✅ " + _fancy("Yes, Demote"), "dm:go", style="danger"),
        B("✖ " + _fancy("Cancel"), "dm:x", style="success"),
    ]])
    sent = await ctx.bot.send_message(
        chat.id, q(T("<b>⚠ demote</b>\n\nremove all admin powers of {m}?", m=st["tgt_m"])), reply_markup=kb,
        reply_parameters=ReplyParameters(message_id=p["msg"].message_id, allow_sending_without_reply=True),
    )
    _put(f"{chat.id}:{sent.message_id}", st)


async def _load(update, mode):
    qy = update.callback_query
    m = qy.message
    key = f"{m.chat_id}:{m.message_id}"
    st = PANELS.get(key)
    if not st or st["mode"] != mode:
        await qy.answer("Panel expired. Run the command again.", show_alert=True)
        return None, None, None
    if qy.from_user.id != st["invoker"]:
        await qy.answer("This panel is not for you.", show_alert=True)
        return None, None, None
    return qy, st, key


async def _verify_now(ctx, qy, st):
    cid = st["chat_id"]
    inv = await get_member(ctx, cid, qy.from_user.id)
    if not inv or inv.status not in (OWNER, ADMIN) or (inv.status == ADMIN and not inv.can_promote_members):
        await qy.answer("You no longer have the Add Admins right.", show_alert=True)
        return None
    br = rights_of(await get_member(ctx, cid, ctx.bot.id))
    if not br["promote_members"]:
        await qy.answer("I don't have the Add New Admins power.", show_alert=True)
        await tag_missing(ctx, cid, qy.from_user, ["Add New Admins"])
        return None
    return br


# ───────────── Anonymous Verification Callback ─────────────

async def anon_verify_callback(update, ctx):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data not in ANON_PENDING:
        await query.edit_message_text("This verification link is no longer valid.")
        return

    st = ANON_PENDING[data]
    chat_id = st["chat_id"]
    real_owner_id = await _get_real_group_owner_id(ctx, chat_id)

    if query.from_user.id != real_owner_id and query.from_user.id != BOT_OWNER_ID:
        await query.answer("❌ Only the real group owner can approve this!", show_alert=True)
        return

    bm = await get_member(ctx, chat_id, ctx.bot.id)
    br = rights_of(bm)
    if not br["promote_members"]:
        await query.edit_message_text(T("I don't have the Add New Admins power."))
        return

    target = await get_member(ctx, chat_id, st["target_id"])
    if not target or target.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        await query.edit_message_text(T("That user is not in this group."))
        return

    chat = await ctx.bot.get_chat(chat_id)
    user = query.from_user

    rights_list = [(k, l) for k, l in RIGHTS if k != "manage_topics" or chat.is_forum]
    if target.status == ADMIN:
        cur = rights_of(target)
        sel = {k: cur[k] and br[k] for k, _ in rights_list}
        sel["anonymous"] = bool(target.is_anonymous)
    else:
        sel = {k: (k in ("delete_messages", "invite_users", "pin_messages")) and br[k] for k, _ in rights_list}

    panel_st = dict(
        mode=st["mode"], chat_id=chat.id, invoker=user.id, target=target.user.id,
        tgt_m=mention(target.user), inv_m=mention(user), title=st["title"],
        rights_list=rights_list, bot=br, sel=sel, forum=bool(chat.is_forum),
        missing=[l for k, l in rights_list if not br[k]],
        page=0,
    )

    await query.edit_message_text(q(_panel_text(panel_st)), reply_markup=_panel_kb(panel_st), parse_mode=ParseMode.HTML)
    _put(f"{chat.id}:{query.message.message_id}", panel_st)
    ANON_PENDING.pop(data, None)


# ───────────── Regular Callbacks ─────────────

async def promote_cb(update, ctx):
    qy, st, key = await _load(update, "promote")
    if not st:
        return
    parts = qy.data.split(":")
    action = parts[1]
    arg = parts[2] if len(parts) > 2 else None

    # ── Nav: noop ──
    if action == "noop":
        await qy.answer()
        return

    # ── Nav: prev ──
    if action == "prev":
        st["page"] = max(0, st.get("page", 0) - 1)
        try:
            await qy.edit_message_text(q(_panel_text(st)), reply_markup=_panel_kb(st))
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
        await qy.answer()
        return

    # ── Nav: next ──
    if action == "next":
        items = _all_items(st)
        total_pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
        st["page"] = min(total_pages - 1, st.get("page", 0) + 1)
        try:
            await qy.edit_message_text(q(_panel_text(st)), reply_markup=_panel_kb(st))
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
        await qy.answer()
        return

    if action == "na":
        await qy.answer("❌ I don't have this power. Give it to me first.", show_alert=True)
        return
    if action == "x":
        PANELS.pop(key, None)
        await qy.edit_message_text(q(T("promotion cancelled ✖")))
        await qy.answer()
        return
    if action == "t":
        if arg != "anonymous" and not st["bot"].get(arg):
            await qy.answer("❌ I don't have this power.", show_alert=True)
            return
        st["sel"][arg] = not st["sel"].get(arg)
    elif action == "full":
        for k, _ in st["rights_list"]:
            st["sel"][k] = bool(st["bot"].get(k))
        st["sel"]["anonymous"] = True
    elif action == "clear":
        st["sel"] = {}
    elif action == "go":
        br = await _verify_now(ctx, qy, st)
        if br is None:
            return
        chosen = [k for k, _ in st["rights_list"] if st["sel"].get(k)]
        lack = [RL[k] for k in chosen if not br.get(k)]
        if lack:
            await qy.answer("I lack some selected powers.", show_alert=True)
            await tag_missing(ctx, st["chat_id"], qy.from_user, lack)
            return
        if not chosen:
            await qy.answer("Select at least one power.", show_alert=True)
            return
        kw = {f"can_{k}": (k in chosen) for k, _ in st["rights_list"]}
        try:
            await ctx.bot.promote_chat_member(st["chat_id"], st["target"], can_manage_chat=True, is_anonymous=bool(st["sel"].get("anonymous")), **kw)
        except TelegramError as e:
            await qy.answer("Failed, see message below.")
            await say(ctx, st["chat_id"], f"{st['inv_m']}, " + T("promotion failed:") + f" {explain(e)}")
            return
        note = ""
        if st["title"]:
            try:
                await ctx.bot.set_chat_administrator_custom_title(st["chat_id"], st["target"], st["title"])
                note = "\n" + T("title: ") + f"<b>{esc(st['title'])}</b>"
            except TelegramError as e:
                note = "\n" + T("title not set:") + f" {explain(e)}"
        powers = "\n".join(f"✅ {RL[k]}" for k in chosen)
        if st["sel"].get("anonymous"):
            powers += "\n✅ Anonymous"
        PANELS.pop(key, None)
        await qy.edit_message_text(q(T("<b>✅ promoted</b>\n\n{m}\n\n{p}{n}", m=st["tgt_m"], p=powers, n=note)))
        await qy.answer("Promoted ✅")

        # ── logchannel ──
        try:
            _tm = await ctx.bot.get_chat_member(st["chat_id"], st["target"])
            _target_name = _tm.user.full_name
        except TelegramError:
            _target_name = "User"
        _chat_title = qy.message.chat.title or ""
        _chosen_names = ", ".join(RL[k] for k in chosen)
        if st["sel"].get("anonymous"):
            _chosen_names += ", Anonymous"
        await logchannel.log_action(
            ctx.bot, st["chat_id"], "PROMOTE",
            chat_title=_chat_title,
            admin_id=qy.from_user.id, admin_name=qy.from_user.full_name,
            user_id=st["target"], user_name=_target_name,
            extra_lines=[f"<b>Powers:</b> {esc(_chosen_names)}"],
        )
        return
    try:
        await qy.edit_message_text(q(_panel_text(st)), reply_markup=_panel_kb(st))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise
    await qy.answer()


async def demote_cb(update, ctx):
    qy, st, key = await _load(update, "demote")
    if not st:
        return
    action = qy.data.split(":")[1]
    if action == "x":
        PANELS.pop(key, None)
        await qy.edit_message_text(q(T("demotion cancelled ✖")))
        await qy.answer()
        return
    br = await _verify_now(ctx, qy, st)
    if br is None:
        return
    kw = {f"can_{k}": False for k, _ in RIGHTS if k != "manage_topics" or st["forum"]}
    try:
        await ctx.bot.promote_chat_member(st["chat_id"], st["target"], can_manage_chat=False, is_anonymous=False, **kw)
    except TelegramError as e:
        await qy.answer("Failed, see message below.")
        await say(ctx, st["chat_id"], f"{st['inv_m']}, " + T("demotion failed:") + f" {explain(e)}")
        return
    PANELS.pop(key, None)
    await qy.edit_message_text(q(T("<b>✅ demoted</b>\n\n{m} is no longer an admin.", m=st["tgt_m"])))
    await qy.answer("Demoted ✅")

    # ── logchannel ──
    try:
        _tm = await ctx.bot.get_chat_member(st["chat_id"], st["target"])
        _target_name = _tm.user.full_name
    except TelegramError:
        _target_name = "User"
    _chat_title = qy.message.chat.title or ""
    await logchannel.log_action(
        ctx.bot, st["chat_id"], "DEMOTE",
        chat_title=_chat_title,
        admin_id=qy.from_user.id, admin_name=qy.from_user.full_name,
        user_id=st["target"], user_name=_target_name,
        reason="",
        )


def register(app):
    dual_command(app, "promote", promote_cmd)
    dual_command(app, "demote", demote_cmd)
    app.add_handler(CallbackQueryHandler(promote_cb, pattern=r"^pr:"))
    app.add_handler(CallbackQueryHandler(demote_cb, pattern=r"^dm:"))
    app.add_handler(CallbackQueryHandler(anon_verify_callback, pattern=r"^anon_"))
    
                  
