"""Admin plugin: /promote and /demote with a live power-selection panel (Rich Messages UI)."""
from collections import OrderedDict
import inspect

from telegram import ChatPermissions, InlineKeyboardMarkup, ReplyParameters, Bot, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import CallbackQueryHandler, ContextTypes

import logchannel
from common import (
    ADMIN, B, OWNER, T, dual_command, esc, explain, get_member,
    mention, q, require_admin, resolve_target, rights_of, say, tag_missing,
)

# ⚠️ APNI TELEGRAM ID YAHAN DAALEIN
BOT_OWNER_ID = 8373739674

# Telegram's official Anonymous Admin Bot ID
ANON_ADMIN_ID = 1087968824

HELP_TXT = (
    "<b>✦ admin — promote &amp; demote</b>\n\n"
    "/promote (or .promote) — reply / @username / id (+ optional custom title). "
    "a panel opens with paginated buttons: tap to toggle each power, "
    "⬅️/➡️ to change pages, ✨ Confirm to promote.\n"
    "/demote (or .demote) — removes all admin powers after confirmation\n\n"
    "every action is verified: you need add admins, and i must have it too. "
    "🔒 buttons = powers i don't have."
)

COMMANDS = [("promote", "Promote a user"), ("demote", "Demote an admin")]

PANELS: "OrderedDict[str, dict]" = OrderedDict()
ANON_PENDING = {}

# ───────────────────── PAGE DEFINITIONS ─────────────────────
PAGES = [
    # Page 1
    [
        ("change_info", "Change Info", ["can_change_info"]),
        ("delete_messages", "Delete Msgs", ["can_delete_messages"]),
        ("invite_users", "Invite Users", ["can_invite_users"]),
        ("restrict_members", "Ban Users", ["can_restrict_members"]),
    ],
    # Page 2
    [
        ("pin_messages", "Pin Msgs", ["can_pin_messages"]),
        ("manage_chat", "Manage Chat", ["can_manage_chat"]),
        ("manage_video_chats", "Video Chats", ["can_manage_video_chats"]),
        ("manage_topics", "Topics", ["can_manage_topics"]),
    ],
    # Page 3
    [
        ("stories", "Stories", ["can_post_stories", "can_edit_stories", "can_delete_stories"]),
        ("manage_tags", "Member Tags", ["can_manage_tags"]),
        ("send_welcome", "Welcome", ["can_send_welcome_messages"]),
        ("promote_members", "Add Admins", ["can_promote_members"]),
    ],
]

_ALL_RIGHTS = [item for page in PAGES for item in page]


def _has_all(member, params):
    """True only if member has ALL the api params set."""
    try:
        return all(bool(getattr(member, p, False)) for p in params)
    except Exception:
        return False


def _valid_promote_params():
    try:
        sig = inspect.signature(Bot.promote_chat_member)
        params = set(sig.parameters.keys())
        for p in sig.parameters.values():
            if p.kind == inspect.Parameter.VAR_KEYWORD:
                return None
        return params
    except Exception:
        return None


_VALID_PARAMS = _valid_promote_params()


def _filter_kwargs(kw):
    if _VALID_PARAMS is None:
        return dict(kw)
    return {k: v for k, v in kw.items() if k in _VALID_PARAMS}


# ───────────────────── FANCY FONT ─────────────────────
_FANCY_MAP = {}
for _n, _f in zip("abcdefghijklmnopqrstuvwxyz",
                  "𝒂𝒃𝒄𝒅𝒆𝒇𝒈𝒉𝒊𝒋𝒌𝒍𝒎𝒏𝒐𝒑𝒒𝒓𝒔𝒕𝒖𝒗𝒘𝒙𝒚𝒛"):
    _FANCY_MAP[_n] = _f
for _n, _f in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                  "𝑨𝑩𝑪𝑫𝑬𝑭𝑮𝑯𝑰𝑱𝑲𝑳𝑴𝑵𝑶𝑷𝑸𝑹𝑺𝑻𝑼𝑽𝑾𝑿𝒀𝒁"):
    _FANCY_MAP[_n] = _f


def _fancy(s: str) -> str:
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

    if user.id == ANON_ADMIN_ID:
        target, title = await resolve_target(update, ctx)
        if not target:
            await say(ctx, chat.id, T("reply to a user, or use /{c} @username | user id", c=mode), reply_to=msg.message_id)
            return None

        action_id = f"anon_{mode}_{chat.id}_{msg.message_id}"
        ANON_PENDING[action_id] = {
            "mode": mode, "chat_id": chat.id, "target_id": target.id,
            "target_name": target.first_name, "title": title[:16] if title else "",
            "invoker_msg_id": msg.message_id,
        }

        kb = InlineKeyboardMarkup([[B("𝙥𝙧𝙤𝙫𝙚 𝙊𝙬𝙣𝙚𝙧/𝙖𝙙𝙢𝙞𝙣", action_id, style="success")]])
        await ctx.bot.send_message(
            chat.id,
            q(T("<b>⚠️ Anonymous Admin detected.</b>\n"
                "Only the real group owner can approve this. "
                "Tap the button below to verify.")),
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
    if not br.get("promote_members"):
        if not bm or bm.status not in (ADMIN, OWNER):
            await say(ctx, chat.id, T("{m}, i am not an admin here. make me admin with the {l} power first.",
                                      m=mention(user), l="<b>Add New Admins</b>"), reply_to=msg.message_id)
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
        await say(ctx, chat.id, T("{m} is an admin promoted by someone else, so i can't edit their powers. only that admin or the owner can.",
                                  m=mention(target)), reply_to=msg.message_id)
        return None
    if mode == "demote" and tm.status != ADMIN:
        await say(ctx, chat.id, T("{m} is not an admin.", m=mention(target)), reply_to=msg.message_id)
        return None
    return dict(chat=chat, user=user, target=target, title=title[:16], tm=tm, br=br, msg=msg, bot_member=bm)


# ───────────────────── RICH PANEL BUILDER ─────────────────────

def _build_rich_panel(st) -> dict:
    page = st.get("page", 0)
    total_pages = len(PAGES)
    current_page_rights = PAGES[page]

    fancy_select = _fancy("Select Admin Rights for")
    fancy_page = _fancy("Page")
    header_text = f"{fancy_select} {st.get('tgt_name', 'User')}\n{fancy_page} {page + 1}/{total_pages}"

    blocks = [
        {
            "type": "blockquote",
            "blocks": [{"type": "paragraph", "text": header_text}]
        }
    ]

    btn_row = []
    for key, label, params in current_page_rights:
        on = bool(st["sel"].get(key))
        bot_has = _has_all(st["bot_member"], params)

        if not bot_has and any(p in ("can_manage_tags", "can_send_welcome_messages") for p in params):
            bot_has = True

        fancy_label = _fancy(label)

        if not bot_has:
            btn = {"text": fancy_label, "callback_data": f"pr:na:{key}", "style": "danger"}
        else:
            btn = {"text": fancy_label, "callback_data": f"pr:t:{key}", "style": "success" if on else "danger"}
        btn_row.append(btn)

        if len(btn_row) == 2:
            blocks.append({"type": "buttons", "buttons": btn_row})
            btn_row = []

    blocks.append({"type": "divider"})

    nav_btns = []
    if page > 0:
        nav_btns.append({"text": _fancy("Previous"), "callback_data": "pr:prev", "style": "primary"})
    else:
        nav_btns.append({"text": _fancy("Previous"), "callback_data": "pr:noop", "style": "primary"})

    if page < total_pages - 1:
        nav_btns.append({"text": _fancy("Next"), "callback_data": "pr:next", "style": "primary"})
    else:
        nav_btns.append({"text": _fancy("Next"), "callback_data": "pr:noop", "style": "primary"})

    blocks.append({"type": "buttons", "buttons": nav_btns})
    blocks.append({"type": "divider"})

    blocks.append({
        "type": "buttons",
        "buttons": [
            {"text": _fancy("Confirm"), "callback_data": "pr:go", "style": "success"},
            {"text": _fancy("Cancel"), "callback_data": "pr:x", "style": "danger"},
        ]
    })

    return {"blocks": blocks}


def _build_demote_panel(target_name: str) -> dict:
    """Confirmation panel for /demote (shared by normal + anonymous-admin flow)."""
    return {
        "blocks": [
            {
                "type": "blockquote",
                "blocks": [{"type": "paragraph", "text": f"Remove all admin powers of {target_name}?"}],
            },
            {
                "type": "buttons",
                "buttons": [
                    {"text": _fancy("Yes, Demote"), "callback_data": "dm:go", "style": "danger"},
                    {"text": _fancy("Cancel"), "callback_data": "dm:x", "style": "success"},
                ],
            },
        ]
    }


async def _send_rich_message(bot, chat_id, rich_message, reply_to_message_id=None):
    kwargs = {"chat_id": chat_id, "rich_message": rich_message}
    if reply_to_message_id:
        kwargs["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
    return await bot.do_api_request("send_rich_message", api_kwargs=kwargs)


async def _edit_rich_message(bot, chat_id, message_id, rich_message):
    kwargs = {"chat_id": chat_id, "message_id": message_id, "rich_message": rich_message}
    return await bot.do_api_request("edit_message_text", api_kwargs=kwargs)


# ───────────────────── PROMOTE / DEMOTE COMMANDS ─────────────────────

async def promote_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    p = await _precheck(update, ctx, "promote")
    if not p: return
    chat, user, target, tm, br, bm = p["chat"], p["user"], p["target"], p["tm"], p["br"], p["bot_member"]

    sel = {}
    if tm.status == ADMIN:
        for key, _, params in _ALL_RIGHTS:
            sel[key] = _has_all(tm, params) and _has_all(bm, params)
    else:
        defaults = ("delete_messages", "invite_users", "pin_messages")
        for key, _, params in _ALL_RIGHTS:
            sel[key] = (key in defaults) and _has_all(bm, params)

    st = dict(
        mode="promote", chat_id=chat.id, invoker=user.id, target=target.id,
        tgt_m=mention(target), tgt_name=target.full_name, inv_m=mention(user), title=p["title"],
        bot_member=bm, sel=sel, forum=bool(chat.is_forum), page=0,
    )

    rich_msg = _build_rich_panel(st)
    sent = await _send_rich_message(ctx.bot, chat.id, rich_msg, reply_to_message_id=p["msg"].message_id)

    msg_id = sent.get("message_id") if isinstance(sent, dict) else getattr(sent, "message_id", None)
    if msg_id: _put(f"{chat.id}:{msg_id}", st)
    else: await say(ctx, chat.id, "Failed to send rich message panel. Please try again.")


async def demote_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    p = await _precheck(update, ctx, "demote")
    if not p: return
    chat, user, target, tm = p["chat"], p["user"], p["target"], p["tm"]

    # 👇 yahan tgt_is_bot save kar rahe hain — bypass ke liye zaruri hai
    st = dict(
        mode="demote", chat_id=chat.id, invoker=user.id, target=target.id,
        tgt_m=mention(target), tgt_name=target.full_name, inv_m=mention(user),
        forum=bool(chat.is_forum),
        tgt_is_bot=bool(getattr(tm.user, "is_bot", False)),
    )

    sent = await _send_rich_message(ctx.bot, chat.id, _build_demote_panel(target.full_name),
                                     reply_to_message_id=p["msg"].message_id)
    msg_id = sent.get("message_id") if isinstance(sent, dict) else getattr(sent, "message_id", None)
    if msg_id: _put(f"{chat.id}:{msg_id}", st)


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
    bm = await get_member(ctx, cid, ctx.bot.id)
    br = rights_of(bm)
    if not br.get("promote_members"):
        await qy.answer("I don't have the Add New Admins power.", show_alert=True)
        await tag_missing(ctx, cid, qy.from_user, ["Add New Admins"])
        return None
    return bm


# ───────────── Anonymous Verification ─────────────

async def anon_verify_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
    if not br.get("promote_members"):
        await query.edit_message_text(T("I don't have the Add New Admins power."))
        return

    target = await get_member(ctx, chat_id, st["target_id"])
    if not target or target.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        await query.edit_message_text(T("That user is not in this group."))
        return

    chat = await ctx.bot.get_chat(chat_id)
    user = query.from_user

    # ── DEMOTE branch (alag handle karo) ──
    if st["mode"] == "demote":
        if target.status == OWNER:
            await query.edit_message_text(T("that user is the group owner. nothing can be changed."))
            return
        if target.status != ADMIN:
            await query.edit_message_text(T("that user is not an admin."))
            return
        demote_st = dict(
            mode="demote", chat_id=chat.id, invoker=user.id, target=target.user.id,
            tgt_m=mention(target.user), tgt_name=target.user.full_name, inv_m=mention(user),
            forum=bool(chat.is_forum),
            tgt_is_bot=bool(getattr(target.user, "is_bot", False)),
        )
        await _edit_rich_message(ctx.bot, chat.id, query.message.message_id,
                                 _build_demote_panel(demote_st["tgt_name"]))
        _put(f"{chat.id}:{query.message.message_id}", demote_st)
        ANON_PENDING.pop(data, None)
        return

    # ── PROMOTE branch ──
    sel = {}
    if target.status == ADMIN:
        for key, _, params in _ALL_RIGHTS:
            sel[key] = _has_all(target, params) and _has_all(bm, params)
    else:
        defaults = ("delete_messages", "invite_users", "pin_messages")
        for key, _, params in _ALL_RIGHTS:
            sel[key] = (key in defaults) and _has_all(bm, params)

    panel_st = dict(
        mode=st["mode"], chat_id=chat.id, invoker=user.id, target=target.user.id,
        tgt_m=mention(target.user), tgt_name=target.user.full_name, inv_m=mention(user), title=st["title"],
        bot_member=bm, sel=sel, forum=bool(chat.is_forum), page=0,
    )
    rich_msg = _build_rich_panel(panel_st)
    await _edit_rich_message(ctx.bot, chat.id, query.message.message_id, rich_msg)
    _put(f"{chat.id}:{query.message.message_id}", panel_st)
    ANON_PENDING.pop(data, None)


# ───────────── Regular Callbacks ─────────────

async def promote_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    qy, st, key = await _load(update, "promote")
    if not st: return
    parts = qy.data.split(":")
    action = parts[1]
    arg = parts[2] if len(parts) > 2 else None

    if action == "noop":
        await qy.answer()
        return

    if action == "prev":
        st["page"] = max(0, st.get("page", 0) - 1)
        rich_msg = _build_rich_panel(st)
        try: await _edit_rich_message(ctx.bot, st["chat_id"], qy.message.message_id, rich_msg)
        except BadRequest as e:
            if "not modified" not in str(e).lower(): raise
        await qy.answer()
        return

    if action == "next":
        st["page"] = min(len(PAGES) - 1, st.get("page", 0) + 1)
        rich_msg = _build_rich_panel(st)
        try: await _edit_rich_message(ctx.bot, st["chat_id"], qy.message.message_id, rich_msg)
        except BadRequest as e:
            if "not modified" not in str(e).lower(): raise
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
        params = None
        bot_has = False
        for page_rights in PAGES:
            for k, _, p in page_rights:
                if k == arg:
                    params = p
                    bot_has = _has_all(st["bot_member"], p)
                    break
            if params: break
        if params is None:
            await qy.answer()
            return
        if not bot_has:
            if any(p in ("can_manage_tags", "can_send_welcome_messages") for p in params):
                bot_has = True
            else:
                await qy.answer("❌ I don't have this power.", show_alert=True)
                return
        st["sel"][arg] = not st["sel"].get(arg)

    elif action == "go":
        bm = await _verify_now(ctx, qy, st)
        if bm is None: return

        kw = {}
        missing = []
        for k, label, params in _ALL_RIGHTS:
            if not st["sel"].get(k): continue
            if not _has_all(bm, params):
                if not any(p in ("can_manage_tags", "can_send_welcome_messages") for p in params):
                    missing.append(label)
                    continue
            for p in params:
                kw[p] = True
        if missing:
            await qy.answer("I lack some selected powers.", show_alert=True)
            await tag_missing(ctx, st["chat_id"], qy.from_user, missing)
            return

        kw["can_manage_chat"] = True

        # RAW API — library filtering bypass karta hai
        payload = {"chat_id": st["chat_id"], "user_id": st["target"]}
        payload.update(kw)

        skipped_powers = []
        try:
            await ctx.bot.do_api_request("promoteChatMember", api_kwargs=payload)
        except TelegramError:
            stripped = ["can_manage_tags", "can_send_welcome_messages", "can_manage_live_streams",
                        "can_post_stories", "can_edit_stories", "can_delete_stories"]
            fallback_payload = {k: v for k, v in payload.items() if k not in stripped}
            try:
                await ctx.bot.do_api_request("promoteChatMember", api_kwargs=fallback_payload)
                for k, label, params in _ALL_RIGHTS:
                    if st["sel"].get(k) and any(p in stripped for p in params):
                        skipped_powers.append(label)
            except TelegramError as e2:
                await qy.answer("Failed, see message below.")
                await say(ctx, st["chat_id"], f"{st['inv_m']}, " + T("promotion failed:") + f" {explain(e2)}")
                return

        note = ""
        if st["title"]:
            try:
                await ctx.bot.set_chat_administrator_custom_title(st["chat_id"], st["target"], st["title"])
                note = "\n" + T("title: ") + f"<b>{esc(st['title'])}</b>"
            except TelegramError as e:
                note = "\n" + T("title not set:") + f" {explain(e)}"

        powers = []
        for k, label, params in _ALL_RIGHTS:
            if st["sel"].get(k):
                if label in skipped_powers:
                    powers.append(f"❌ {label} (Telegram restricted)")
                else:
                    powers.append(f"✅ {label}")
        powers_text = "\n".join(powers) if powers else "—"

        PANELS.pop(key, None)
        await qy.edit_message_text(q(T("<b>✅ promoted</b>\n\n{m}\n\n{p}{n}",
                                       m=st["tgt_m"], p=powers_text, n=note)))
        await qy.answer("Promoted ✅")

        try:
            _tm = await ctx.bot.get_chat_member(st["chat_id"], st["target"])
            _target_name = _tm.user.full_name
        except TelegramError:
            _target_name = "User"
        _chosen_names = ", ".join(label for k, label, _ in _ALL_RIGHTS if st["sel"].get(k))
        await logchannel.log_action(
            ctx.bot, st["chat_id"], "PROMOTE",
            chat_title=qy.message.chat.title or "",
            admin_id=qy.from_user.id, admin_name=qy.from_user.full_name,
            user_id=st["target"], user_name=_target_name,
            extra_lines=[f"<b>Powers:</b> {esc(_chosen_names)}"],
        )
        return

    rich_msg = _build_rich_panel(st)
    try: await _edit_rich_message(ctx.bot, st["chat_id"], qy.message.message_id, rich_msg)
    except BadRequest as e:
        if "not modified" not in str(e).lower(): raise
    await qy.answer()


# ───────────── DEMOTE BYPASS HELPERS ─────────────

_OPTIONAL_PROMOTE_KEYS = (
    "can_manage_tags", "can_send_welcome_messages", "can_manage_live_streams",
    "can_post_stories", "can_edit_stories", "can_delete_stories",
)


async def _demote_via_restrict(ctx, chat_id: int, target_id: int) -> None:
    """
    🔥 BOT BYPASS 🔥
    Telegram bots ke admin rights edit nahi karne deta (BOT_CHANNELS_NA error).
    Trick:
      1. restrict_chat_member(can_send_messages=False) → admin status drop ho jata hai
      2. restrict_chat_member(all_permissions()) → restrictions hatti, banda normal member ban jata hai
    """
    # Step 1: restrict → admin status hat jata hai
    await ctx.bot.restrict_chat_member(
        chat_id, target_id,
        permissions=ChatPermissions(can_send_messages=False),
    )
    # Step 2: turant unrestrict (all_permissions = "no restrictions" in Bot API)
    try:
        await ctx.bot.restrict_chat_member(
            chat_id, target_id,
            permissions=ChatPermissions.all_permissions(),
        )
    except TelegramError as e:
        raise RuntimeError(
            f"admin powers removed but lifting restriction failed ({e}). "
            "Please unrestrict the user manually."
        ) from e


async def demote_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    qy, st, key = await _load(update, "demote")
    if not st: return
    action = qy.data.split(":")[1]
    if action == "x":
        PANELS.pop(key, None)
        await qy.edit_message_text(q(T("demotion cancelled ✖")))
        await qy.answer()
        return

    bm = await _verify_now(ctx, qy, st)
    if bm is None: return

    chat_id = st["chat_id"]
    target_id = st["target"]
    is_bot = bool(st.get("tgt_is_bot"))

    async def _fail(detail: str):
        await qy.answer("Failed, see message below.")
        await say(ctx, chat_id, f"{st['inv_m']}, " + T("demotion failed:") + f" {detail}")

    # ── STEP 1: normal demote via RAW promoteChatMember (saari powers false) ──
    kw = {}
    for _, _, params in _ALL_RIGHTS:
        for p in params:
            if p == "is_anonymous":
                continue
            kw[p] = False
    kw["can_manage_chat"] = False
    kw["is_anonymous"] = False

    payload = {"chat_id": chat_id, "user_id": target_id}
    payload.update(kw)

    try:
        await ctx.bot.do_api_request("promoteChatMember", api_kwargs=payload)
    except TelegramError as e:
        # purane servers ke liye optional keys strip karke retry
        fallback_payload = {k: v for k, v in payload.items() if k not in _OPTIONAL_PROMOTE_KEYS}
        try:
            await ctx.bot.do_api_request("promoteChatMember", api_kwargs=fallback_payload)
        except TelegramError as e2:
            print(f"[admin] demote failed chat={chat_id} target={target_id} is_bot={is_bot}: "
                  f"{type(e2).__name__}: {e2}")

            # ── STEP 2: BYPASS — agar bot hai to restrict/unrestrict use karo ──
            if not is_bot:
                await _fail(explain(e2))
                return

            print(f"[admin] demote: trying restrict fallback for bot target={target_id}")
            try:
                await _demote_via_restrict(ctx, chat_id, target_id)
            except RuntimeError as e3:
                print(f"[admin] demote fallback incomplete: {e3}")
                await _fail(esc(str(e3)))
                return
            except TelegramError as e3:
                print(f"[admin] demote fallback failed: {type(e3).__name__}: {e3}")
                await _fail(explain(e3))
                return

    # ── STEP 3: verify (API ne ok bola to bhi check karo) ──
    tm = None
    try:
        tm = await ctx.bot.get_chat_member(chat_id, target_id)
    except TelegramError:
        pass
    if tm is not None and tm.status == ADMIN:
        print(f"[admin] demote: request accepted but target={target_id} still admin")
        await _fail(T("telegram accepted the request but the user is still an admin."))
        return

    PANELS.pop(key, None)
    await qy.edit_message_text(q(T("<b>✅ demoted</b>\n\n{m} is no longer an admin.", m=st["tgt_m"])))
    await qy.answer("Demoted ✅")

    _target_name = tm.user.full_name if tm is not None else "User"
    await logchannel.log_action(
        ctx.bot, chat_id, "DEMOTE",
        chat_title=qy.message.chat.title or "",
        admin_id=qy.from_user.id, admin_name=qy.from_user.full_name,
        user_id=target_id, user_name=_target_name,
        reason="",
    )


def register(app):
    dual_command(app, "promote", promote_cmd)
    dual_command(app, "demote", demote_cmd)
    app.add_handler(CallbackQueryHandler(promote_cb, pattern=r"^pr:"))
    app.add_handler(CallbackQueryHandler(demote_cb, pattern=r"^dm:"))
    app.add_handler(CallbackQueryHandler(anon_verify_callback, pattern=r"^anon_"))
        
