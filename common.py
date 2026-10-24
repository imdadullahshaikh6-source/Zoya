"""Shared helpers used by every plugin: styling, buttons, permissions, dot-commands."""
import html
import logging
import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import CommandHandler, MessageHandler, filters

import database as dbase

log = logging.getLogger("bot")
OWNER, ADMIN = ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR

# ───────────────────── QUOTE / STICKER CONFIG ─────────────────────
# Env-driven — override on the VPS via .env (already in .env.example):
#   QUOTE_API=http://127.0.0.1:3000/generate
#   QUOTE_API_FALLBACKS=https://bot.lyo.su/quote/generate
#   QUOTE_BG=#1b1429
#   QUOTE_TIMEOUT=10
#   QUOTE_TOTAL_TIMEOUT=22
QUOTE_API = os.getenv("QUOTE_API", "http://127.0.0.1:3000/generate")
QUOTE_API_FALLBACKS = os.getenv(
    "QUOTE_API_FALLBACKS",
    "https://bot.lyo.su/quote/generate",
)
QUOTE_BG = os.getenv("QUOTE_BG", "#1b1429")
QUOTE_TIMEOUT = float(os.getenv("QUOTE_TIMEOUT", "10"))
QUOTE_TOTAL_TIMEOUT = float(os.getenv("QUOTE_TOTAL_TIMEOUT", "22"))

# ───────────────────── FONT / STYLE ─────────────────────
_SC = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))
assert len(_SC) == 26
# tags, {placeholders}, /commands, .commands and @names are never converted
_SKIP = re.compile(r"(<[^>]+>|\{[^}]*\}|(?<![\w./])[./][a-zA-Z_]+|@\w+)")


def sc(text: str) -> str:
    parts = _SKIP.split(text)
    return "".join(
        p if i % 2 else "".join(_SC.get(ch, ch) for ch in p.lower())
        for i, p in enumerate(parts)
    )


def T(text: str, **kw) -> str:
    """Small-caps static text, then fill {placeholders} with pre-escaped values."""
    return sc(text).format(**kw)


def esc(t) -> str:
    return html.escape(str(t), quote=False)


def q(text: str) -> str:
    """Every bot message goes inside a quote block."""
    return f"<blockquote>{text}</blockquote>"


def mention(u) -> str:
    name = getattr(u, "full_name", None) or str(u.id)
    return f'<a href="tg://user?id={u.id}">{esc(name)}</a>'


_BTN_STYLES = ("primary", "success", "danger")


def make_button(
    text,
    *,
    url=None,
    callback_data=None,
    style=None,
    icon_custom_emoji_id=None,
    disabled=False,
) -> InlineKeyboardButton:
    """
    Single place where every InlineKeyboardButton of the bot is built.

    Bot API fields that older python-telegram-bot builds don't know yet are sent
    through `api_kwargs`, so this works on any PTB version:
      style                 (API 9.4)  'primary' blue | 'success' green | 'danger' red
      icon_custom_emoji_id  (API 9.4)  custom emoji shown before the label
      disabled              (API 10.3) DisabledButton -> a greyed-out button that does nothing
    """
    extra = {}
    style = str(style).lower() if style else None
    if style in _BTN_STYLES:
        extra["style"] = style
    if icon_custom_emoji_id:
        extra["icon_custom_emoji_id"] = str(icon_custom_emoji_id)
    if disabled:
        # DisabledButton "currently holds no information" -> empty object.
        # A disabled button must not carry any action field.
        url = callback_data = None
        extra["disabled"] = {}
    kw = {"api_kwargs": extra} if extra else {}
    return InlineKeyboardButton(text, url=url, callback_data=callback_data, **kw)


def B(text, data=None, url=None, style=None, icon=None, disabled=False) -> InlineKeyboardButton:
    """Backward-compatible shortcut. style: 'primary' | 'success' | 'danger'."""
    return make_button(
        text, url=url, callback_data=data, style=style,
        icon_custom_emoji_id=icon, disabled=disabled,
    )


async def say(ctx, chat_id, text, kb=None, reply_to=None):
    rp = (
        ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        if reply_to
        else None
    )
    return await ctx.bot.send_message(
        chat_id, q(text), parse_mode=ParseMode.HTML, reply_markup=kb, reply_parameters=rp
    )


# ───────────────────── ROSE-STYLE BUTTON PARSER ─────────────────────
# ONE parser for filters.py, welcome.py (welcome + rules) — every format below works everywhere:
#
#   [Name](buttonurl://https://link.com)                              plain button
#   [Name](buttonurl://https://link.com:success)                      colour  (primary | success | danger)
#   [Name](buttonurl://https://link.com:6170217276621465566)          custom-emoji icon
#   [Name](buttonurl://https://link.com:success:6170217276621465566)  colour + icon
#   [Name](buttonurl://https://link.com:same:danger:6170...)          ":same" -> joins the previous row
#   [Name](buttonurl://disabled)                                      disabled button (API 10.3)
#
# Modifiers are read from the END of the tail, in ANY order, so "https://host:8080/x" keeps its port.
#
# TWO button kinds, chosen by the literal word  :style:
#   no ":style:"  -> standard InlineKeyboardMarkup button (style / icon via api_kwargs)
#   with ":style:" -> RICH PANEL button, exactly like /promote in admin.py:
#       [Name](buttonurl://https://link.com:primary:style:6170217276621465566)
#       -> {"type": "buttons", "buttons": [{"text", "url", "style", "icon_custom_emoji_id"}]}
# The parser flags those buttons with "rich": True. Also accepted: style=success, color=green,
# icon=<id>, emoji=<id> (these do NOT make a rich button).
BTN_RE = re.compile(r"\[([^\[\]\n]+?)\]\(\s*buttonurl:\s*/{0,2}\s*([^)\n]+?)\s*\)", re.I)

_STYLE_ALIASES = {
    "primary": "primary", "blue": "primary",
    "success": "success", "green": "success",
    "danger": "danger", "red": "danger",
}
_FILLER = {"icon", "emoji", "color", "colour", "id"}      # harmless words, ignored
# the bare word "style" is NOT ignored: it switches the button to a Rich Panel button
_STYLE_KV_RE = re.compile(r"^(?:style|color|colour)\s*=\s*(\w+)$", re.I)
# custom emoji ids are 64-bit numbers (19 digits in practice). A bare number counts as an emoji id
# from 10 digits up (a port such as ":8080" can never be mistaken for one); icon=<id> / emoji=<id>
# works for any length.
_ICON_KV_RE = re.compile(r"^(?:icon|emoji|icon_custom_emoji_id|custom_emoji_id|id)\s*=\s*(\d+)$", re.I)
_ICON_BARE_RE = re.compile(r"^\d{10,25}$")


def normalize_url(url: str) -> str:
    """'example.com' -> 'https://example.com'. tg://, mailto: and full URLs are kept."""
    url = (url or "").strip()
    if not url:
        return url
    if re.match(r"^[a-zA-Z][\w+.-]*://", url) or url.lower().startswith(("tg:", "mailto:")):
        return url
    return "https://" + url


def url_ok(url: str) -> bool:
    """Cheap sanity check so ONE bad link can never make Telegram reject the whole message."""
    if not url or re.search(r"\s", url):
        return False
    m = re.match(r"^([a-zA-Z][\w+.-]*):(?://)?(.+)$", url)
    if not m:
        return False
    if m.group(1).lower() in ("http", "https"):
        host = re.split(r"[/?#]", m.group(2), maxsplit=1)[0].rsplit("@", 1)[-1].split(":")[0]
        return "." in host.strip(".")
    return True


def parse_button_tail(tail: str) -> dict:
    """
    Parse everything after `buttonurl:` in  [Text](buttonurl://<tail>).
    Returns {"url", "same", "style", "icon", "disabled"}.
    """
    parts = (tail or "").strip().split(":")
    same, style, icon, rich = False, None, None, False
    while len(parts) > 1:
        tok = parts[-1].strip()
        low = tok.lower()
        kv_style = _STYLE_KV_RE.match(tok)
        kv_icon = _ICON_KV_RE.match(tok)
        if not tok or low in _FILLER:
            pass                                    # trailing ":" or a filler word
        elif low == "style":
            rich = True                             # -> Rich Panel button (admin.py style)
        elif low == "same":
            same = True
        elif low in _STYLE_ALIASES:
            style = style or _STYLE_ALIASES[low]
        elif kv_style and kv_style.group(1).lower() in _STYLE_ALIASES:
            style = style or _STYLE_ALIASES[kv_style.group(1).lower()]
        elif kv_icon:
            icon = icon or kv_icon.group(1)
        elif _ICON_BARE_RE.match(tok):
            icon = icon or tok
        else:
            break  # not a modifier -> it belongs to the URL (e.g. https://host:8080/x)
        parts.pop()
    url = ":".join(parts).strip()
    disabled = url.lower() == "disabled"
    return {
        "url": "" if disabled else url,
        "same": same,
        "style": style,
        "icon": icon,
        "disabled": disabled,
        "rich": rich,
    }


def parse_button_match(label: str, tail: str, from_html: bool = True):
    """
    One regex match -> (button_dict | None, same_row).
    button_dict = {"text", "url" | "disabled": True, ["style"], ["icon_custom_emoji_id"]}
    from_html=True  : welcome / rules / legacy filters (text is stored as HTML)
    from_html=False : entity-based filters (plain text, must not be un-escaped)
    """
    if from_html:
        label = html.unescape(re.sub(r"<[^>]*>", "", label))   # button labels can't carry tags
        tail = html.unescape(tail)
    label = label.strip()
    if not label:
        return None, False
    spec = parse_button_tail(tail)
    d = {"text": label}
    if spec["disabled"]:
        d["disabled"] = True
    else:
        url = normalize_url(spec["url"])
        if not url_ok(url):
            return None, False
        d["url"] = url
    if spec["style"]:
        d["style"] = spec["style"]
    if spec["icon"]:
        d["icon_custom_emoji_id"] = spec["icon"]
    if spec["rich"]:
        d["rich"] = True
    return d, spec["same"]


def button_from_dict(d: dict, icons: bool = True, styles: bool = True):
    """Stored/parsed button dict -> InlineKeyboardButton (style + icon go through api_kwargs)."""
    if not d.get("url") and not d.get("disabled"):
        return None
    return make_button(
        d["text"],
        url=d.get("url"),
        style=d.get("style") if styles else None,
        icon_custom_emoji_id=d.get("icon_custom_emoji_id") if icons else None,
        disabled=bool(d.get("disabled")),
    )


def markup_from_rows(rows, icons: bool = True, styles: bool = True):
    """list[list[button_dict]] -> InlineKeyboardMarkup | None."""
    out = []
    for row in rows or []:
        r = [b for b in (button_from_dict(d, icons, styles) for d in row) if b]
        if r:
            out.append(r)
    return InlineKeyboardMarkup(out) if out else None


def add_to_rows(rows, d, same):
    """':same' joins the previous row, but only if that row is the same kind (standard / rich)."""
    if same and rows and bool(rows[-1][0].get("rich")) == bool(d.get("rich")):
        rows[-1].append(d)
    else:
        rows.append([d])


def split_button_rows(rows):
    """-> (standard_rows, rich_rows). A row's kind is the kind of its buttons (':style:' = rich)."""
    std = [r for r in rows or [] if not r[0].get("rich")]
    rich = [r for r in rows or [] if r[0].get("rich")]
    return std, rich


def parse_button_rows(raw: str, from_html: bool = True):
    """Returns (clean_text_without_button_syntax, rows: list[list[button_dict]])."""
    if not raw:
        return raw, []
    rows = []

    def _sub(m):
        d, same = parse_button_match(m.group(1), m.group(2), from_html)
        if not d:
            return m.group(0)          # invalid button -> leave the text visible
        add_to_rows(rows, d, same)
        return ""

    clean = BTN_RE.sub(_sub, raw)
    clean = re.sub(r"[ \t]*\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", clean).strip()
    return clean, rows


def parse_buttons(raw: str, icons: bool = True, styles: bool = True):
    """Returns (clean_text, InlineKeyboardMarkup | None). Rich (':style:') buttons are shown as
    normal inline buttons here; use parse_button_rows + split_button_rows to keep them rich."""
    clean, rows = parse_button_rows(raw)
    return clean, markup_from_rows(rows, icons, styles)


# ───────────────────── RICH PANEL BUTTONS (':style:' buttons, like admin.py) ─────────────────────

def rich_button_dict(d: dict, icons: bool = True, styles: bool = True) -> dict:
    """{"text", "url", "style", "icon_custom_emoji_id"} — same keys admin.py uses in its panel."""
    out = {"text": d["text"]}
    if d.get("disabled"):
        out["disabled"] = {}
    else:
        out["url"] = d["url"]
    if styles and d.get("style"):
        out["style"] = d["style"]
    if icons and d.get("icon_custom_emoji_id"):
        out["icon_custom_emoji_id"] = str(d["icon_custom_emoji_id"])
    return out


def rich_button_blocks(rich_rows, icons: bool = True, styles: bool = True) -> list:
    """One {"type": "buttons", "buttons": [...]} block per button row."""
    return [
        {"type": "buttons", "buttons": [rich_button_dict(d, icons, styles) for d in row]}
        for row in rich_rows or [] if row
    ]


def with_rich_buttons(rich: dict, rich_rows, icons: bool = True, styles: bool = True) -> dict:
    """Copy of a rich message with the button blocks appended at the END."""
    out = dict(rich or {})
    out["blocks"] = list(out.get("blocks") or []) + rich_button_blocks(rich_rows, icons, styles)
    return out


def html_to_plain(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    return html.unescape(re.sub(r"<[^>]*>", "", text)).strip()


def text_to_rich(text: str, html_mode: bool = False) -> dict:
    """Plain/HTML text -> rich message (one paragraph block per blank-line-separated chunk).
    Rich paragraphs are plain text, so bold/italic/custom-emoji entities of the source are lost."""
    if html_mode:
        text = html_to_plain(text)
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    return {"blocks": [{"type": "paragraph", "text": p} for p in paras]}


def extract_rich_buttons(rich):
    """
    Scan every string inside a Rich Message for [Label](buttonurl://...) syntax.
    Returns (rich_without_syntax, rows). Text blocks that become empty are dropped.
    """
    rows = []

    def walk(o):
        if isinstance(o, str):
            if "buttonurl" not in o.lower():
                return o
            clean, found = parse_button_rows(o, from_html=False)
            rows.extend(found)
            return clean
        if isinstance(o, list):
            out = []
            for x in o:
                nx = walk(x)
                if (
                    isinstance(x, dict) and isinstance(x.get("text"), str)
                    and "buttonurl" in x["text"].lower()
                    and not (nx.get("text") or "").strip()
                    and not any(k in nx for k in ("blocks", "buttons", "items"))
                ):
                    continue
                out.append(nx)
            return out
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        return o

    return walk(rich or {}), rows


# Telegram refuses a custom-emoji icon unless the bot owner has Premium (or the bot owns Fragment
# usernames). Rather than losing the whole message, we retry: full -> no icons -> no icons/colours.
BUTTON_LADDER = ((True, True), (False, True), (False, False))      # (icons, styles)


def is_button_extra_error(e) -> bool:
    s = str(e).lower()
    return any(k in s for k in ("emoji", "icon", "style", "button"))


async def send_with_fallback(send, rows):
    """`send(kb)` is an async callable. Tries the rich buttons first, then simpler versions."""
    variants, seen = [], set()
    for icons, styles in BUTTON_LADDER:
        kb = markup_from_rows(rows, icons, styles)
        key = repr(kb.to_dict()) if kb else None
        if key not in seen:
            seen.add(key)
            variants.append(kb)
    for i, kb in enumerate(variants):
        try:
            return await send(kb)
        except BadRequest as e:
            if i == len(variants) - 1 or not is_button_extra_error(e):
                raise
            log.warning("buttons rejected (%s) - retrying with simpler buttons", e)


# ───────────────────── RICH MESSAGES (Bot API 10.1+) ─────────────────────
# Shared by filters.py and welcome.py.

def get_rich_message(message):
    """Message.rich_message as a plain dict (older PTB keeps it in api_kwargs / to_dict())."""
    r = getattr(message, "rich_message", None)
    if r is not None:
        return r.to_dict() if hasattr(r, "to_dict") else (dict(r) if isinstance(r, dict) else None)
    try:
        r = message.to_dict().get("rich_message")
    except Exception:
        r = None
    return r if isinstance(r, dict) and r else None


def scrub_rich(o):
    """Received RichMessage -> sendable shape: media objects become bare file_id strings."""
    if isinstance(o, dict):
        if "file_id" in o:
            return o["file_id"]
        return {k: scrub_rich(v) for k, v in o.items() if k != "file_unique_id"}
    if isinstance(o, list):
        if o and all(isinstance(x, dict) and "file_id" in x for x in o):   # PhotoSize[] -> biggest
            return o[-1]["file_id"]
        return [scrub_rich(x) for x in o]
    return o


def rich_plain_text(o) -> str:
    """Last-resort fallback: join every 'text' string of a rich message."""
    out = []

    def walk(x, in_text=False):
        if isinstance(x, str):
            if in_text:
                out.append(x)
        elif isinstance(x, dict):
            for k, v in x.items():
                if k != "type":
                    walk(v, in_text or k in ("text", "caption", "title"))
        elif isinstance(x, list):
            for i in x:
                walk(i, in_text)

    walk(o)
    return "\n".join(t for t in out if t.strip())


def map_strings(o, fn):
    """Apply fn to every string inside a nested dict/list (used for {first}-style fillings)."""
    if isinstance(o, str):
        return fn(o)
    if isinstance(o, list):
        return [map_strings(x, fn) for x in o]
    if isinstance(o, dict):
        return {k: map_strings(v, fn) for k, v in o.items()}
    return o


async def _raw_api(bot, method, payload):
    """Raw Bot API call (PTB has no sendRichMessage wrapper yet)."""
    fn = getattr(bot, "do_api_request", None)
    if fn:
        return await fn(method, api_kwargs=payload)
    return await bot._post(method, data=payload)          # very old PTB


def _as_message(res, bot):
    """do_api_request may hand back the raw JSON dict; callers (e.g. cleanwelcome) need .message_id."""
    if isinstance(res, dict):
        try:
            return Message.de_json(res, bot)
        except Exception:
            from types import SimpleNamespace
            return SimpleNamespace(message_id=res.get("message_id"), raw=res)
    return res


async def send_rich(bot, chat_id, row, kb=None, reply_to=None, thread_id=None,
                    rich_rows=None, fallback_kb=None):
    """
    Send a saved Rich Message (row["rich"] = raw object, row["src_chat"/"src_msg"] = original).
    rich_rows : ':style:' buttons -> appended as {"type": "buttons"} blocks (like admin.py's panel);
                tried with icons+colours, then without icons, then without colours.
    kb        : normal inline keyboard (reply_markup).
    fallback_kb: used only if sendRichMessage fails (copy / plain-text fallback), so buttons survive.
    Order: 1) sendRichMessage  2) copyMessage of the original  3) plain text.
    Never calls sendRichMessage with empty content.
    """
    rich = scrub_rich(row.get("rich") or {})
    inp = {k: rich[k] for k in ("blocks", "is_rtl") if k in rich}
    if not inp.get("blocks"):
        inp = rich            # unknown shape: send as received, API will tell us if it dislikes it
    rp = (
        ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        if reply_to else None
    )

    variants = []
    if rich_rows:
        for icons, styles in BUTTON_LADDER:
            v = with_rich_buttons(inp, rich_rows, icons, styles)
            if v not in variants:
                variants.append(v)
    elif inp:
        variants = [inp]

    if variants:
        for i, v in enumerate(variants):
            payload = {"chat_id": chat_id, "rich_message": v}
            if reply_to:
                payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            if kb:
                payload["reply_markup"] = kb.to_dict()
            if thread_id:
                payload["message_thread_id"] = thread_id
            try:
                return _as_message(await _raw_api(bot, "sendRichMessage", payload), bot)
            except Exception as e:
                log.warning("sendRichMessage failed (%s)", e)
                if i == len(variants) - 1 or not is_button_extra_error(e):
                    break
        log.warning("trying copyMessage")
    else:
        log.warning("saved rich message has no content - trying copyMessage")

    fb_kb = kb or fallback_kb
    if row.get("src_chat") and row.get("src_msg"):
        try:
            return await bot.copy_message(
                chat_id=chat_id, from_chat_id=row["src_chat"], message_id=row["src_msg"],
                reply_markup=fb_kb, reply_parameters=rp, message_thread_id=thread_id,
            )
        except Exception as e:
            log.warning("copyMessage failed (%s) - falling back to plain text", e)

    # "\u3164" alone is rejected as empty by Telegram, so use a visible character here.
    txt = (rich_plain_text(row.get("rich")) or "✦")[:4096]
    return await bot.send_message(
        chat_id, txt, reply_markup=fb_kb, reply_parameters=rp, message_thread_id=thread_id,
    )


# ───────────────────── PERMISSIONS ─────────────────────
RIGHTS = [
    ("change_info", "Change Info"),
    ("delete_messages", "Delete Messages"),
    ("restrict_members", "Ban Users"),
    ("invite_users", "Invite Users"),
    ("pin_messages", "Pin Messages"),
    ("manage_video_chats", "Video Chats"),
    ("manage_topics", "Topics"),
    ("promote_members", "Add Admins"),
]
RL = dict(RIGHTS)


async def get_member(ctx, chat_id, user_id):
    try:
        return await ctx.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return None


def rights_of(m) -> dict:
    if m is None:
        return {k: False for k, _ in RIGHTS}
    if m.status == OWNER:
        return {k: True for k, _ in RIGHTS}
    if m.status == ADMIN:
        return {k: bool(getattr(m, f"can_{k}", False)) for k, _ in RIGHTS}
    return {k: False for k, _ in RIGHTS}


async def perm_report(ctx, chat) -> str:
    m = await get_member(ctx, chat.id, ctx.bot.id)
    if not m or m.status not in (ADMIN, OWNER):
        return T("<b>bot status:</b> not admin ❌\nmake me admin so every feature works.")
    r = rights_of(m)
    lines = [T("<b>bot status:</b> admin ✅")]
    for k, label in RIGHTS:
        if k == "manage_topics" and not chat.is_forum:
            continue
        lines.append(f"{'✅' if r[k] else '❌'} {label}")
    return "\n".join(lines)


async def require_admin(update, ctx, right=None) -> bool:
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        await say(ctx, chat.id, T("this command works only in groups."))
        return False
    if msg.sender_chat and msg.sender_chat.id == chat.id:  # anonymous admin
        return True
    user = update.effective_user
    m = await get_member(ctx, chat.id, user.id)
    if not m or m.status not in (OWNER, ADMIN):
        await say(ctx, chat.id, T("only group admins can use this command."), reply_to=msg.message_id)
        return False
    if right and m.status == ADMIN and not getattr(m, f"can_{right}", False):
        await say(
            ctx, chat.id,
            T("{m}, you don't have the {l} right.", m=mention(user), l=f"<b>{RL[right]}</b>"),
            reply_to=msg.message_id,
        )
        return False
    return True


async def tag_missing(ctx, chat_id, user, labels, reply_to=None):
    await say(
        ctx, chat_id,
        T(
            "{m}, i don't have the {l} power. please give it to me and try again.",
            m=mention(user), l="<b>" + esc(", ".join(labels)) + "</b>",
        ),
        reply_to=reply_to,
    )


def explain(e: Exception) -> str:
    s = str(e).lower()
    if "not enough rights" in s or "chat_admin_required" in s or "right_forbidden" in s:
        return "I don't have enough rights for this."
    if "user_creator" in s or "chat owner" in s or "can't remove chat owner" in s:
        return "This user is the group owner. Nothing can be changed."
    if "user_not_participant" in s or "participant_id_invalid" in s:
        return "This user is not in the group."
    return f"Telegram said: {esc(e)}"


# ───────────────────── TARGET RESOLUTION (reply / @username / id) ─────────────────────
async def resolve_target(update, ctx):
    msg, chat = update.effective_message, update.effective_chat
    args = list(ctx.args or [])
    rep = msg.reply_to_message
    if rep and rep.from_user and not rep.sender_chat:
        return rep.from_user, " ".join(args).strip()
    if not args:
        return None, ""
    title = " ".join(args[1:]).strip()
    for ent in msg.entities or []:
        if ent.type == "text_mention" and ent.user:
            return ent.user, title
    a, uid = args[0], None
    if a.lstrip("-").isdigit():
        uid = int(a)
    elif a.startswith("@"):
        uid = await dbase.get_user_id_by_username(a)
        if uid is None:
            try:
                cchat = await ctx.bot.get_chat(a)
                if cchat.type == ChatType.PRIVATE:
                    uid = cchat.id
            except TelegramError as e:
                # Fallback: Check if the user is a member of the current chat using the username
                try:
                    member = await ctx.bot.get_chat_member(chat.id, a)
                    if member and member.user:
                        uid = member.user.id
                except TelegramError as e2:
                    # Both global and local lookups failed
                    log.warning(f"[Resolve] Failed to resolve {a} globally ({e}) or locally ({e2})")
                    pass
    if uid is None:
        return None, ""
    m = await get_member(ctx, chat.id, uid)
    return (m.user if m else None), title


# ───────────────────── DUAL /command AND .command SUPPORT ─────────────────────
def _dotify(callback):
    async def wrapper(update, ctx):
        msg = update.effective_message
        text = msg.text or msg.caption or ""
        ctx.args = text.split()[1:]
        await callback(update, ctx)
    return wrapper


def dual_command(app, names, callback, group_only=True):
    """Registers callback for /name (and .name) — .name works alongside / everywhere it's added."""
    if isinstance(names, str):
        names = [names]
    f = filters.ChatType.GROUPS if group_only else filters.ALL
    app.add_handler(CommandHandler(names, callback, filters=f))
    pattern = re.compile(
        r"^\.(?:%s)(?:@\w+)?(?:\s|$)" % "|".join(re.escape(n) for n in names), re.I
    )
    app.add_handler(MessageHandler(filters.Regex(pattern) & f, _dotify(callback)))


def human_delta(seconds: float) -> str:
    seconds = int(seconds)
    d, seconds = divmod(seconds, 86400)
    h, seconds = divmod(seconds, 3600)
    m, seconds = divmod(seconds, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts[:2])
