"""Shared helpers used by every plugin: styling, buttons, permissions, dot-commands."""
import copy
import html
import json
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
QUOTE_API = os.getenv("QUOTE_API", "http://127.0.0.1:3000/generate")
QUOTE_API_FALLBACKS = os.getenv("QUOTE_API_FALLBACKS", "https://bot.lyo.su/quote/generate")
QUOTE_BG = os.getenv("QUOTE_BG", "#1b1429")
QUOTE_TIMEOUT = float(os.getenv("QUOTE_TIMEOUT", "10"))
QUOTE_TOTAL_TIMEOUT = float(os.getenv("QUOTE_TOTAL_TIMEOUT", "22"))

# ───────────────────── FONT / STYLE ─────────────────────
_SC = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))
assert len(_SC) == 26
_SKIP = re.compile(r"(<[^>]+>|\{[^}]*\}|(?<![\w./])[./][a-zA-Z_]+|@\w+)")


def sc(text: str) -> str:
    parts = _SKIP.split(text)
    return "".join(
        p if i % 2 else "".join(_SC.get(ch, ch) for ch in p.lower())
        for i, p in enumerate(parts)
    )


def T(text: str, **kw) -> str:
    return sc(text).format(**kw)


def esc(t) -> str:
    return html.escape(str(t), quote=False)


def q(text: str) -> str:
    return f"<blockquote>{text}</blockquote>"


def mention(u) -> str:
    name = getattr(u, "full_name", None) or str(u.id)
    return f'<a href="tg://user?id={u.id}">{esc(name)}</a>'


_BTN_STYLES = ("primary", "success", "danger")


def make_button(
    text, *, url=None, callback_data=None, style=None, icon_custom_emoji_id=None, disabled=False,
) -> InlineKeyboardButton:
    extra = {}
    style = str(style).lower() if style else None
    if style in _BTN_STYLES:
        extra["style"] = style
    if icon_custom_emoji_id:
        extra["icon_custom_emoji_id"] = str(icon_custom_emoji_id)
    if disabled:
        url = callback_data = None
        extra["disabled"] = {}
    kw = {"api_kwargs": extra} if extra else {}
    return InlineKeyboardButton(text, url=url, callback_data=callback_data, **kw)


def B(text, data=None, url=None, style=None, icon=None, disabled=False) -> InlineKeyboardButton:
    return make_button(
        text, url=url, callback_data=data, style=style,
        icon_custom_emoji_id=icon, disabled=disabled,
    )


async def say(ctx, chat_id, text, kb=None, reply_to=None):
    rp = (
        ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        if reply_to else None
    )
    return await ctx.bot.send_message(
        chat_id, q(text), parse_mode=ParseMode.HTML, reply_markup=kb, reply_parameters=rp
    )


# ───────────────────── ROSE-STYLE BUTTON PARSER ─────────────────────
BTN_RE = re.compile(r"\[([^\[\]\n]+?)\]\(\s*buttonurl:\s*/{0,2}\s*([^)]+?)\s*\)", re.I | re.S)

_STYLE_ALIASES = {
    "primary": "primary", "blue": "primary",
    "success": "success", "green": "success",
    "danger": "danger", "red": "danger",
}
_FILLER = {"icon", "emoji", "color", "colour", "id"}
_STYLE_KV_RE = re.compile(r"^(?:style|color|colour)\s*=\s*(\w+)$", re.I)
_ICON_KV_RE = re.compile(r"^(?:icon|emoji|icon_custom_emoji_id|custom_emoji_id|id)\s*=\s*(\d+)$", re.I)
_ICON_BARE_RE = re.compile(r"^\d{10,25}$")


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    if re.match(r"^[a-zA-Z][\w+.-]*://", url) or url.lower().startswith(("tg:", "mailto:")):
        return url
    return "https://" + url


def url_ok(url: str) -> bool:
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
    parts = (tail or "").strip().split(":")
    same, style, icon, rich = False, None, None, False
    while len(parts) > 1:
        tok = parts[-1].strip()
        low = tok.lower()
        kv_style = _STYLE_KV_RE.match(tok)
        kv_icon = _ICON_KV_RE.match(tok)
        if not tok or low in _FILLER:
            pass
        elif low == "style":
            rich = True
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
            break
        parts.pop()
    url = ":".join(parts).strip()
    disabled = url.lower() == "disabled"
    return {
        "url": "" if disabled else url, "same": same, "style": style, "icon": icon,
        "disabled": disabled, "rich": rich,
    }


def parse_button_match(label: str, tail: str, from_html: bool = True):
    emoji_id = None
    if from_html:
        # Extract custom emoji ID from label if present
        emoji_match = re.search(r'<tg-emoji[^>]+emoji-id=["\']?(\d+)["\']?', label)
        if not emoji_match:
            emoji_match = re.search(r'<emoji[^>]+id=["\']?(\d+)["\']?', label)
        if emoji_match:
            emoji_id = emoji_match.group(1)
        
        label = html.unescape(re.sub(r"<[^>]*>", "", label))
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
    
    # Use explicit icon from tail, else use extracted from label
    if spec["icon"]:
        d["icon_custom_emoji_id"] = spec["icon"]
    elif emoji_id:
        d["icon_custom_emoji_id"] = emoji_id
        
    if spec["rich"]:
        d["rich"] = True
    return d, spec["same"]


def button_from_dict(d: dict, icons: bool = True, styles: bool = True):
    if not d.get("url") and not d.get("disabled"):
        return None
    return make_button(
        d["text"], url=d.get("url"), style=d.get("style") if styles else None,
        icon_custom_emoji_id=d.get("icon_custom_emoji_id") if icons else None,
        disabled=bool(d.get("disabled")),
    )


def markup_from_rows(rows, icons: bool = True, styles: bool = True):
    out = []
    for row in rows or []:
        r = [b for b in (button_from_dict(d, icons, styles) for d in row) if b]
        if r:
            out.append(r)
    return InlineKeyboardMarkup(out) if out else None


def add_to_rows(rows, d, same):
    if same and rows and bool(rows[-1][0].get("rich")) == bool(d.get("rich")):
        rows[-1].append(d)
    else:
        rows.append([d])


def split_button_rows(rows):
    std = [r for r in rows or [] if not r[0].get("rich")]
    rich = [r for r in rows or [] if r[0].get("rich")]
    return std, rich


def parse_button_rows(raw: str, from_html: bool = True):
    if not raw:
        return raw, []
    rows = []

    def _sub(m):
        d, same = parse_button_match(m.group(1), m.group(2), from_html)
        if not d:
            return m.group(0)
        add_to_rows(rows, d, same)
        return ""

    clean = BTN_RE.sub(_sub, raw)
    clean = re.sub(r"[ \t]*\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", clean).strip()
    return clean, rows


def parse_buttons(raw: str, icons: bool = True, styles: bool = True):
    clean, rows = parse_button_rows(raw)
    return clean, markup_from_rows(rows, icons, styles)


def rich_button_dict(d: dict, icons: bool = True, styles: bool = True) -> dict:
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
    return [
        {"type": "buttons", "buttons": [rich_button_dict(d, icons, styles) for d in row]}
        for row in rich_rows or [] if row
    ]


def with_rich_buttons(rich: dict, rich_rows, icons: bool = True, styles: bool = True) -> dict:
    out = dict(rich or {})
    out["blocks"] = list(out.get("blocks") or []) + rich_button_blocks(rich_rows, icons, styles)
    return out


def html_to_plain(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    return html.unescape(re.sub(r"<[^>]*>", "", text)).strip()


RICH_BLANK = "⚡"

def ensure_rich_nonempty(rich: dict) -> dict:
    rich = dict(rich or {})
    if not rich.get("blocks"):
        rich["blocks"] = [{"type": "paragraph", "text": RICH_BLANK}]
    else:
        has_text = False
        def check_text(blocks):
            nonlocal has_text
            if isinstance(blocks, list):
                for b in blocks:
                    if isinstance(b, dict):
                        if b.get("type") in ("paragraph", "blockquote", "expandable_blockquote"):
                            has_text = True
                        if "blocks" in b:
                            check_text(b["blocks"])
        check_text(rich["blocks"])
        if not has_text:
            rich["blocks"].append({"type": "paragraph", "text": RICH_BLANK})
    return rich


def text_to_rich(text: str, html_mode: bool = False) -> dict:
    if html_mode:
        text = html_to_plain(text)
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    return ensure_rich_nonempty({"blocks": [{"type": "paragraph", "text": p} for p in paras]})


_RT_KEYS = ("text", "caption", "credit", "summary", "title")


def _rt_leaves(o, parent, key, out):
    if isinstance(o, str):
        out.append((parent, key))
    elif isinstance(o, list):
        for i, x in enumerate(o):
            _rt_leaves(x, o, i, out)
    elif isinstance(o, dict):
        for k, v in o.items():
            if k == "text" or isinstance(v, (list, dict)):
                _rt_leaves(v, o, k, out)


def _rt_prune(o):
    if isinstance(o, str):
        return o or None
    if isinstance(o, list):
        kept = [y for y in (_rt_prune(x) for x in o) if y is not None]
        return kept or None
    if isinstance(o, dict) and "text" in o:
        t = _rt_prune(o["text"])
        if t is None:
            return None
        o = dict(o)
        o["text"] = t
    return o


def _rt_extract(container, key, rows) -> bool:
    leaves = []
    _rt_leaves(container[key], container, key, leaves)
    texts = [p[k] for p, k in leaves]
    flat = "".join(texts)
    if "buttonurl" not in flat.lower():
        return False
    cut, last_end = [], 0
    for m in BTN_RE.finditer(flat):
        d, same = parse_button_match(m.group(1), m.group(2), from_html=False)
        if not d:
            continue
        add_to_rows(rows, d, same)
        st, en = m.start(), m.end()
        if st > last_end and st > 0 and flat[st - 1] == "\n":
            st -= 1
        cut.append((st, en))
        last_end = en
    if not cut:
        return False
    keep = [True] * len(flat)
    for st, en in cut:
        for i in range(st, en):
            keep[i] = False
    pos, new_texts = 0, []
    for t in texts:
        new_texts.append("".join(ch for i, ch in enumerate(t, pos) if keep[i]))
        pos += len(t)
    for i in range(len(new_texts) - 1, -1, -1):
        stripped = new_texts[i].rstrip()
        new_texts[i] = stripped
        if stripped:
            break
    for (par, k), t in zip(leaves, new_texts):
        par[k] = t
    pruned = _rt_prune(container[key])
    if pruned is None:
        container[key] = RICH_BLANK
    else:
        container[key] = pruned
    return True


def extract_rich_buttons(rich):
    rich = copy.deepcopy(rich or {})
    rows = []

    def walk(o):
        if isinstance(o, list):
            before = len(o)
            o[:] = [x for x in o if not walk(x)]
            return before > 0 and not o
        if isinstance(o, dict):
            had = False
            for k in list(o):
                if k in _RT_KEYS:
                    if _rt_extract(o, k, rows):
                        had = True
                elif isinstance(o[k], (list, dict)):
                    if walk(o[k]):
                        had = True
            if had and (set(o) <= {"type"} or o.get("blocks") == []):
                return True
        return False

    walk(rich)
    if rows and "blocks" in rich:
        rich = ensure_rich_nonempty(rich)
    return rich, rows


BUTTON_LADDER = ((True, True), (False, True), (False, False))


def is_button_extra_error(e) -> bool:
    s = str(e).lower()
    return any(k in s for k in ("emoji", "icon", "style", "button"))


async def send_with_fallback(send, rows):
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


def get_rich_message(message):
    r = getattr(message, "rich_message", None)
    if r is not None:
        return r.to_dict() if hasattr(r, "to_dict") else (dict(r) if isinstance(r, dict) else None)
    try:
        r = message.to_dict().get("rich_message")
    except Exception:
        r = None
    return r if isinstance(r, dict) and r else None


def scrub_rich(o):
    if isinstance(o, dict):
        if "file_id" in o:
            return o["file_id"]
        return {k: scrub_rich(v) for k, v in o.items() if k != "file_unique_id"}
    if isinstance(o, list):
        if o and all(isinstance(x, dict) and "file_id" in x for x in o):
            return o[-1]["file_id"]
        return [scrub_rich(x) for x in o]
    return o


def rich_plain_text(o) -> str:
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
    if isinstance(o, str):
        return fn(o)
    if isinstance(o, list):
        return [map_strings(x, fn) for x in o]
    if isinstance(o, dict):
        return {k: map_strings(v, fn) for k, v in o.items()}
    return o


async def _raw_api(bot, method, payload):
    fn = getattr(bot, "do_api_request", None)
    if fn:
        return await fn(method, api_kwargs=payload)
    return await bot._post(method, data=payload)


def _as_message(res, bot):
    if isinstance(res, dict):
        try:
            return Message.de_json(res, bot)
        except Exception:
            from types import SimpleNamespace
            return SimpleNamespace(message_id=res.get("message_id"), raw=res)
    return res


async def send_rich(bot, chat_id, row, kb=None, reply_to=None, thread_id=None,
                    rich_rows=None, fallback_kb=None):
    rich = scrub_rich(row.get("rich") or {})
    inp = {k: rich[k] for k in ("blocks", "is_rtl") if k in rich}
    if not inp.get("blocks"):
        inp = rich
    
    inp = ensure_rich_nonempty(inp)
    
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
                res = await _raw_api(bot, "sendRichMessage", payload)
                if rich_rows:
                    log.warning("[rich-buttons] sent %d button row(s) as rich blocks (variant %d)",
                                len(rich_rows), i + 1)
                return _as_message(res, bot)
            except Exception as e:
                log.warning("sendRichMessage failed (%s) payload=%s", e,
                            json.dumps(v, ensure_ascii=False, default=str)[:1800])
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
    if msg.sender_chat and msg.sender_chat.id == chat.id:
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


# ───────────────────── TARGET RESOLUTION ─────────────────────
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
                try:
                    member = await ctx.bot.get_chat_member(chat.id, a)
                    if member and member.user:
                        uid = member.user.id
                except TelegramError as e2:
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


# ───────────────────── TEXT TRUNCATION ─────────────────────
def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def truncate_text_entities(text: str, entities: list, max_len: int = 1024):
    if _u16(text) <= max_len:
        return text, entities
    
    out = []
    u16_len = 0
    for ch in text:
        ch_len = _u16(ch)
        if u16_len + ch_len > max_len:
            break
        out.append(ch)
        u16_len += ch_len
    truncated = "".join(out)
    
    new_ents = []
    for ent in entities or []:
        if ent["offset"] >= max_len:
            continue
        ent_len = min(ent["length"], max_len - ent["offset"])
        if ent_len > 0:
            d = dict(ent)
            d["length"] = ent_len
            new_ents.append(d)
    return truncated, new_ents
    
