"""Blocklist — per-chat banned words.

• Admin adds words  → bot saves them in MongoDB.
• Anyone (member OR admin) says a blocklisted word → message is deleted.
• If /blocklistmode is mute / ban / kick → the user also gets that action
  (admins can't be punished by Telegram, so for them it's delete only).
• Works with "/" and "." prefix, groups only.
"""
import asyncio
import re
import unicodedata

from telegram import ChatPermissions
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import MessageHandler, filters

import database as dbase
from common import log, mention, sc

HELP_TXT = (
    "<b>🚫 𝘽𝙡𝙤𝙘𝙠𝙡𝙞𝙨𝙩</b>\n\n"
    "Want to stop people from using certain words? Blocklist is the module for you!\n"
    "Any message containing a blocklisted word is deleted automatically — "
    "for members and admins both.\n\n"
    "<b>Admin commands:</b>\n"
    "• <code>/blacklist</code> (or <code>.blacklist</code>) &lt;word(s)&gt; — add words. "
    "Put a full sentence in \"quotes\".\n"
    "• <code>/addblocklist</code> (or <code>.addblocklist</code>) (reply) — reply to a message "
    "that has a list (one word per line) and the bot saves every word of it.\n"
    "• <code>/rmblocklist</code> (or <code>.rmblocklist</code>) &lt;word(s)&gt; — remove words.\n"
    "• <code>/blocklist</code> (or <code>.blocklist</code>) — list all blocklisted words.\n"
    "• <code>/blocklistmode</code> (or <code>.blocklistmode</code>) &lt;nothing/mute/ban/kick&gt; — "
    "action to take with the delete. Default: nothing (only delete).\n"
    "• <code>/unblocklistall</code> (or <code>.unblocklistall</code>) — remove all words — chat creator only.\n\n"
    "<b>Top tip:</b>\n"
    "• <code>*</code> works as a wildcard: <code>porn*</code> matches porn, porno, pornhub.\n"
    "• Words are matched as whole words, so <code>web</code> will not delete <i>website</i>.\n\n"
    "<i>Note: the bot must be an admin with 'Delete Messages' right "
    "(and 'Restrict Members' for mute / ban / kick).</i>"
)

COMMANDS = [
    ("blocklist", "List blocklisted words"),
    ("addblocklist", "Add words to the blocklist"),
    ("rmblocklist", "Remove words from the blocklist"),
    ("blocklistmode", "Set action for blocklisted words"),
]

ADD_CMDS = {"addblocklist", "addblacklist", "blacklist"}
RM_CMDS = {"rmblocklist", "rmblacklist", "unblocklist", "unblacklist"}
CLEAR_CMDS = {"unblocklistall", "unblacklistall"}
LIST_CMDS = {"blocklist", "blocklists", "blacklists"}
MODE_CMDS = {"blocklistmode", "blacklistmode"}
ALL_CMDS = ADD_CMDS | RM_CMDS | CLEAR_CMDS | LIST_CMDS | MODE_CMDS

CMD_RE = re.compile(
    r"^[/.](" + "|".join(sorted(ALL_CMDS, key=len, reverse=True)) + r")(?:@(\w+))?(?=\s|$)(.*)",
    re.I | re.S,
)

MODES = {"nothing": "nothing", "none": "nothing", "off": "nothing",
         "mute": "mute", "ban": "ban", "kick": "kick"}

MAX_WORDS = 1000
MIN_LEN, MAX_LEN = 2, 64
ADMIN_STATUS = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)

# chat_id -> {"words": [...], "mode": str, "rx": compiled | None}
_cache: dict = {}
_tasks: set = set()


# ───────────────────────── helpers ─────────────────────────

def _fmt(text: str) -> str:
    return f"<blockquote>{sc(text)}</blockquote>"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_ZW = re.compile("[\u200b-\u200f\u2060\ufeff\u00ad]")


def norm(s: str) -> str:
    """lowercase + NFKC (turns 𝐟𝐚𝐧𝐜𝐲 letters into normal ones) + strip zero-width chars."""
    s = unicodedata.normalize("NFKC", s)
    s = _ZW.sub("", s)
    return " ".join(s.lower().split())


_TOKEN = re.compile(r'["“”]([^"“”]+)["“”]|([^\s,;]+)')
_BULLET = re.compile(r"^\s*(?:[-•·▪●]+|\d+[.)])\s+")


def parse_words(text: str) -> list:
    """Multi-line text → one word/phrase per line. Single line → split by space ("quotes" = phrase)."""
    lines = [l for l in (text or "").splitlines() if l.strip()]
    raw = []
    if len(lines) > 1:
        raw = [_BULLET.sub("", l).strip().strip('"“”') for l in lines]
    elif lines:
        line = _BULLET.sub("", lines[0])
        raw = [m.group(1) or m.group(2) for m in _TOKEN.finditer(line)]
    out, seen = [], set()
    for w in raw:
        w = norm(w).strip('"“”')
        if MIN_LEN <= len(w) <= MAX_LEN and w not in seen:
            seen.add(w)
            out.append(w)
    return out


_WCH = r"[\w\u0900-\u097F]"


def build_rx(words: list):
    if not words:
        return None
    parts = []
    for w in sorted(words, key=len, reverse=True):
        p = re.escape(w).replace(r"\*", r"\w*").replace(r"\ ", r"\s+").replace(" ", r"\s+")
        parts.append(p)
    try:
        return re.compile(rf"(?<!{_WCH})(?:{'|'.join(parts)})(?!{_WCH})", re.I)
    except re.error as e:
        log.warning("[blocklist] regex build failed: %s", e)
        return re.compile(rf"(?<!{_WCH})(?:{'|'.join(re.escape(w) for w in words)})(?!{_WCH})", re.I)


async def _load(chat_id: int) -> dict:
    data = _cache.get(chat_id)
    if data is None:
        doc = await dbase.blocklist_get(chat_id)
        words = list(doc.get("words", []))
        data = {"words": words, "mode": doc.get("mode", "nothing"), "rx": build_rx(words)}
        _cache[chat_id] = data
    return data


def _refresh(data: dict):
    data["rx"] = build_rx(data["words"])


async def _status(ctx, chat_id: int, user_id: int):
    try:
        m = await ctx.bot.get_chat_member(chat_id, user_id)
        return m.status
    except TelegramError:
        return None


async def _reply(msg, text: str):
    try:
        return await msg.reply_text(_fmt(text))
    except TelegramError as e:
        log.warning("[blocklist] reply failed: %s", e)


def _later_delete(m, delay: int = 15):
    if not m:
        return

    async def _run():
        await asyncio.sleep(delay)
        try:
            await m.delete()
        except TelegramError:
            pass

    t = asyncio.create_task(_run())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _chunks(lines: list, limit: int = 3000):
    buf, size = [], 0
    for l in lines:
        if size + len(l) + 1 > limit and buf:
            yield "\n".join(buf)
            buf, size = [], 0
        buf.append(l)
        size += len(l) + 1
    if buf:
        yield "\n".join(buf)


# ───────────────────────── commands ─────────────────────────

async def _cmd(update, ctx):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    m = CMD_RE.match(msg.text or "")
    if not m:
        return
    cmd, target, rest = m.group(1).lower(), m.group(2), m.group(3) or ""
    me = ctx.application.bot_data.get("me")
    if target and me and target.lower() != (me.username or "").lower():
        return  # command meant for another bot

    anon = bool(msg.sender_chat and msg.sender_chat.id == chat.id)
    status = None
    if not anon and user:
        status = await _status(ctx, chat.id, user.id)
    if not (anon or status in ADMIN_STATUS):
        if msg.text.startswith("/"):
            await _reply(msg, "only admins can use this command.")
        return

    data = await _load(chat.id)

    # ── list ──
    if cmd in LIST_CMDS:
        if not data["words"]:
            await _reply(msg, "<b>🚫 blocklist is empty.</b>\n\nadd words with <code>/blacklist word</code>")
            return
        mode = data["mode"]
        lines = [f"• <code>{_esc(w)}</code>" for w in sorted(data["words"])]
        head = f"<b>🚫 blocklisted words ({len(lines)})</b>\n<b>mode:</b> {mode}\n"
        for i, part in enumerate(_chunks(lines)):
            await _reply(msg, (head + "\n" if i == 0 else "") + part)
        return

    # ── mode ──
    if cmd in MODE_CMDS:
        arg = rest.strip().split()[0].lower() if rest.strip() else ""
        if arg not in MODES:
            await _reply(
                msg,
                f"<b>current mode:</b> {data['mode']}\n\n"
                "usage: <code>/blocklistmode nothing/mute/ban/kick</code>\n"
                "<i>nothing = only delete the message.</i>",
            )
            return
        data["mode"] = MODES[arg]
        await dbase.blocklist_set_mode(chat.id, data["mode"])
        extra = "only the message will be deleted." if data["mode"] == "nothing" \
            else f"message will be deleted and the user will be {'muted' if data['mode']=='mute' else 'banned' if data['mode']=='ban' else 'kicked'}."
        await _reply(msg, f"<b>✅ blocklist mode set to:</b> {data['mode']}\n{extra}")
        return

    # ── clear all (creator only) ──
    if cmd in CLEAR_CMDS:
        if status != ChatMemberStatus.OWNER:
            await _reply(msg, "only the chat creator can remove the whole blocklist.")
            return
        n = await dbase.blocklist_clear(chat.id)
        data["words"] = []
        _refresh(data)
        await _reply(msg, f"<b>🗑 blocklist cleared.</b> removed {n} word(s).")
        return

    # ── add / remove ──
    src = rest.strip()
    if not src and msg.reply_to_message:
        src = msg.reply_to_message.text or msg.reply_to_message.caption or ""
    words = parse_words(src)

    if not words:
        if cmd in ADD_CMDS:
            await _reply(
                msg,
                "<b>usage:</b>\n"
                "• <code>/blacklist word</code>\n"
                "• <code>/blacklist word1 word2 \"a full sentence\"</code>\n"
                "• reply to a list (one word per line) with <code>/addblocklist</code>",
            )
        else:
            await _reply(msg, "usage: <code>/rmblocklist word</code>")
        return

    if cmd in ADD_CMDS:
        have = set(data["words"])
        new = [w for w in words if w not in have]
        room = MAX_WORDS - len(data["words"])
        skipped_full = 0
        if len(new) > room:
            skipped_full = len(new) - max(room, 0)
            new = new[:max(room, 0)]
        if new:
            await dbase.blocklist_add(chat.id, new)
            data["words"].extend(new)
            _refresh(data)
        out = []
        if new:
            shown = "\n".join(f"• <code>{_esc(w)}</code>" for w in new[:30])
            more = f"\n<i>…and {len(new) - 30} more</i>" if len(new) > 30 else ""
            out.append(f"<b>✅ added {len(new)} word(s) to the blocklist:</b>\n{shown}{more}")
        dup = len(words) - len(new) - skipped_full
        if dup > 0:
            out.append(f"<i>{dup} word(s) were already in the list.</i>")
        if skipped_full:
            out.append(f"<i>limit reached ({MAX_WORDS} words) — {skipped_full} word(s) not added.</i>")
        await _reply(msg, "\n\n".join(out))
        return

    # remove
    have = set(data["words"])
    gone = [w for w in words if w in have]
    miss = [w for w in words if w not in have]
    if gone:
        await dbase.blocklist_remove(chat.id, gone)
        data["words"] = [w for w in data["words"] if w not in set(gone)]
        _refresh(data)
    out = []
    if gone:
        out.append("<b>✅ removed:</b>\n" + "\n".join(f"• <code>{_esc(w)}</code>" for w in gone[:30]))
    if miss:
        out.append("<i>not in the list:</i>\n" + "\n".join(f"• <code>{_esc(w)}</code>" for w in miss[:30]))
    await _reply(msg, "\n\n".join(out))


# ───────────────────────── message scanner ─────────────────────────

async def _scan(update, ctx):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat:
        return
    text = msg.text or msg.caption
    if not text:
        return
    if getattr(msg, "is_automatic_forward", False):
        return

    anon = bool(msg.sender_chat and msg.sender_chat.id == chat.id)
    if user and user.is_bot and not anon:
        return

    data = await _load(chat.id)
    rx = data["rx"]
    if not rx or not rx.search(norm(text)):
        return

    status = None
    if not anon and user:
        status = await _status(ctx, chat.id, user.id)
    is_admin = anon or status in ADMIN_STATUS

    # admin managing the list (e.g. "/rmblocklist porn") must not be deleted
    if is_admin and CMD_RE.match(text):
        return

    try:
        await msg.delete()
    except TelegramError as e:
        log.warning("[blocklist] delete failed in %s: %s", chat.id, e)

    mode = data["mode"]
    if mode == "nothing" or is_admin or not user or msg.sender_chat:
        return

    try:
        if mode == "mute":
            await ctx.bot.restrict_chat_member(chat.id, user.id, ChatPermissions(can_send_messages=False))
            done = "muted"
        elif mode == "ban":
            await ctx.bot.ban_chat_member(chat.id, user.id)
            done = "banned"
        else:
            await ctx.bot.ban_chat_member(chat.id, user.id)
            await ctx.bot.unban_chat_member(chat.id, user.id, only_if_banned=True)
            done = "kicked"
    except TelegramError as e:
        log.warning("[blocklist] %s failed in %s: %s", mode, chat.id, e)
        return

    try:
        note = await ctx.bot.send_message(
            chat.id,
            _fmt(f"<b>🚫 blocklist</b>\n{mention(user)} has been {done} for using a blocklisted word."),
        )
        _later_delete(note, 15)
    except TelegramError:
        pass


def register(app):
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.UpdateType.MESSAGE & filters.TEXT & filters.Regex(CMD_RE),
            _cmd,
        ),
        group=30,
    )
    # catches new messages AND edited messages (so editing a word in later doesn't bypass it)
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION) & ~filters.StatusUpdate.ALL,
            _scan,
        ),
        group=31,
    )
