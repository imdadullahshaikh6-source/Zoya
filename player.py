"""Voice-chat playback via PyTgCalls.

⚠️ This file is the one part of the whole music bot that could not be tested
here (no internet/Telegram access in the build environment, and voice-chat
streaming can only really be verified against a live group call). PyTgCalls'
API has changed across versions, so if `MediaStream`/`AudioQuality` or the
`.play()` / `.leave_call()` / `.pause()` / `.resume()` calls below don't match
your installed pytgcalls version, check its changelog for your pinned version
— the rest of the bot (queue handling, invite flow, UI) doesn't depend on
these specifics and needs no changes."""
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioQuality, MediaStream

pytgcalls: PyTgCalls | None = None  # set by main.py once the assistant client is running


def init(instance: PyTgCalls):
    global pytgcalls
    pytgcalls = instance


async def play(chat_id: int, stream_url: str):
    """Starts (or replaces) the audio stream for this chat. PyTgCalls handles
    joining the voice chat automatically on the first .play() call for a chat
    it isn't already in."""
    await pytgcalls.play(chat_id, MediaStream(stream_url, audio_parameters=AudioQuality.STUDIO))


async def leave(chat_id: int):
    try:
        await pytgcalls.leave_call(chat_id)
    except Exception:
        pass  # already not in a call — nothing to do


async def pause(chat_id: int):
    await pytgcalls.pause(chat_id)


async def resume(chat_id: int):
    await pytgcalls.resume(chat_id)
  
