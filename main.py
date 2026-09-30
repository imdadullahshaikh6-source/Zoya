"""Entry point: two Pyrogram clients (the bot itself, and the assistant
userbot that actually joins/streams into the voice chat), wired to PyTgCalls."""
import asyncio
import logging

from pyrogram import Client, filters, idle
from pyrogram.errors import UserAlreadyParticipant
from pyrogram.types import CallbackQuery, Message
from pytgcalls import PyTgCalls

import config
import player
import queue_manager as qm
import resolver
import ui

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
log = logging.getLogger("music")

bot = Client("music_bot", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)
assistant = Client("assistant", api_id=config.API_ID, api_hash=config.API_HASH, session_string=config.STRING_SESSION)
calls = PyTgCalls(assistant)
player.init(calls)

CMD = dict(prefixes=["/", "."])


async def _ensure_assistant_in_chat(chat_id: int):
    status = await bot.send_message(chat_id, "🔄 Inviting assistant...")
    try:
        link = await bot.export_chat_invite_link(chat_id)
        await assistant.join_chat(link)
    except UserAlreadyParticipant:
        pass
    except Exception as e:
        await status.edit(f"⚠ couldn't invite the assistant: {e}")
        raise
    finally:
        try:
            await status.delete()
        except Exception:
            pass


async def _send_now_playing(chat_id: int, track):
    q = qm.get(chat_id)
    caption = ui.now_playing_caption(track)
    if track.thumbnail:
        sent = await bot.send_photo(
            chat_id, track.thumbnail, has_spoiler=True,
            caption=caption, parse_mode=ui.PARSE_MODE, reply_markup=ui.bar(),
        )
    else:
        sent = await bot.send_message(chat_id, caption, parse_mode=ui.PARSE_MODE, reply_markup=ui.bar())
    q.panel = (sent.chat.id, sent.id)


async def _do_skip(chat_id: int):
    q = qm.get(chat_id)
    q.pop_current()
    if q.current:
        await player.play(chat_id, q.current.stream_url)
        await _send_now_playing(chat_id, q.current)
    else:
        await player.leave(chat_id)


@bot.on_message(filters.command(["play", "playforce"], **CMD) & filters.group)
async def play_cmd(_, message: Message):
    if len(message.command) < 2:
        await message.reply("usage: .play <song name / youtube link / spotify link>")
        return
    query = message.text.split(None, 1)[1]
    chat_id = message.chat.id
    force = message.command[0].lower() == "playforce"

    try:
        await _ensure_assistant_in_chat(chat_id)
    except Exception:
        return

    status = await message.reply("🔎 Picking right song...")
    try:
        tracks = await resolver.resolve(query, message.from_user)
    except Exception as e:
        await status.edit(f"⚠ couldn't find that: {e}")
        return
    await status.delete()

    q = qm.get(chat_id)
    if force:
        q.clear()
    was_empty = q.current is None
    for t in tracks:
        q.add(t)

    if was_empty:
        try:
            await player.play(chat_id, q.current.stream_url)
        except Exception as e:
            await message.reply(f"⚠ couldn't start playback: {e}")
            q.clear()
            return
        await _send_now_playing(chat_id, q.current)
    else:
        await message.reply(f"➕ added {len(tracks)} track(s) to the queue (position {len(q.tracks) - len(tracks) + 1}).")


@bot.on_message(filters.command("skip", **CMD) & filters.group)
async def skip_cmd(_, message: Message):
    await _do_skip(message.chat.id)
    await message.reply("⏭ skipped.")


@bot.on_message(filters.command(["stop", "end"], **CMD) & filters.group)
async def stop_cmd(_, message: Message):
    chat_id = message.chat.id
    await player.leave(chat_id)
    qm.get(chat_id).clear()
    await message.reply("⏹ stopped and left the voice chat.")


@bot.on_message(filters.command("replay", **CMD) & filters.group)
async def replay_cmd(_, message: Message):
    q = qm.get(message.chat.id)
    if not q.current:
        await message.reply("nothing is playing right now.")
        return
    await player.play(message.chat.id, q.current.stream_url)
    q.paused = False
    await message.reply("🔄 replaying the current track.")


@bot.on_message(filters.command("queue", **CMD) & filters.group)
async def queue_cmd(_, message: Message):
    q = qm.get(message.chat.id)
    if not q.tracks:
        await message.reply("queue is empty.")
        return
    lines = ["<b>Queue</b>"]
    for i, t in enumerate(q.tracks[:config.QUEUE_PREVIEW]):
        tag = "▶️ now" if i == 0 else f"{i}."
        who = t.requested_by.mention if t.requested_by else "someone"
        lines.append(f"{tag} {t.title} — {who}")
    await message.reply("<blockquote>" + "\n".join(lines) + "</blockquote>", parse_mode=ui.PARSE_MODE)


@bot.on_callback_query(filters.regex(r"^ctl:"))
async def controls_cb(_, cq: CallbackQuery):
    action = cq.data.split(":")[1]
    chat_id = cq.message.chat.id
    q = qm.get(chat_id)

    if action == "open":
        await cq.message.edit_reply_markup(ui.bar(q.paused, expanded=True))
        await cq.answer()
        return
    if not q.current:
        await cq.answer("nothing is playing.", show_alert=True)
        return

    if action == "pause":
        if q.paused:
            await player.resume(chat_id)
            q.paused = False
        else:
            await player.pause(chat_id)
            q.paused = True
        await cq.message.edit_reply_markup(ui.bar(q.paused, expanded=True))
        await cq.answer("Resumed ▶" if not q.paused else "Paused ⏸")
    elif action == "replay":
        await player.play(chat_id, q.current.stream_url)
        q.paused = False
        await cq.answer("Replaying 🔄")
    elif action == "stop":
        await player.leave(chat_id)
        q.clear()
        await cq.message.edit_reply_markup(None)
        await cq.answer("Stopped ⏹")
    elif action == "skip":
        await _do_skip(chat_id)
        await cq.answer("Skipped ⏭")


async def _on_stream_end(_, update):
    """Auto-advances the queue when a track finishes on its own (not skipped
    manually). `update.chat_id` matches pytgcalls' documented stream-end
    update shape as of writing — double-check this against your installed
    pytgcalls version if the queue doesn't auto-advance."""
    await _do_skip(update.chat_id)


async def main():
    calls.on_stream_end()(_on_stream_end)
    await assistant.start()
    await bot.start()
    await calls.start()
    log.info("Music bot running.")
    await idle()
    await bot.stop()
    await assistant.stop()


if __name__ == "__main__":
    asyncio.run(main())
  
