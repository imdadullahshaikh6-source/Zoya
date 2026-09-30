"""Now-playing card formatting: a spoilered thumbnail, minimal quoted details,
and a single "Controls ?" button that expands into the playback controls."""
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

BAR_COLLAPSED = InlineKeyboardMarkup([[InlineKeyboardButton("Controls ?", callback_data="ctl:open")]])


def _bar_expanded(paused: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄", callback_data="ctl:replay"),
        InlineKeyboardButton("▶" if paused else "⏸", callback_data="ctl:pause"),
        InlineKeyboardButton("⏹", callback_data="ctl:stop"),
        InlineKeyboardButton("⏭", callback_data="ctl:skip"),
    ]])


def now_playing_caption(track) -> str:
    who = track.requested_by.mention if track.requested_by else "someone"
    return (
        "<blockquote>"
        "🎵 <b>Now Playing</b>\n\n"
        f"Title: {track.title}\n"
        f"Duration: {track.duration_str}\n"
        f"Requested by: {who}"
        "</blockquote>"
    )


def bar(paused: bool = False, expanded: bool = False) -> InlineKeyboardMarkup:
    return _bar_expanded(paused) if expanded else BAR_COLLAPSED


PARSE_MODE = ParseMode.HTML
