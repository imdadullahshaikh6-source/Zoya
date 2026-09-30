"""Resolves a /play query — a search phrase, a YouTube link, or a Spotify
link — into one or more playable Track objects. Spotify links are resolved
to metadata only (Spotify itself is never streamed from); each track/album/
playlist entry is then searched for on YouTube and that result is streamed."""
import asyncio
import re

import yt_dlp

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
except ImportError:
    spotipy = None

import config

_YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

_spotify = None
if spotipy and config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET:
    _spotify = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
        client_id=config.SPOTIFY_CLIENT_ID, client_secret=config.SPOTIFY_CLIENT_SECRET,
    ))

_SPOTIFY_RE = re.compile(r"open\.spotify\.com/(track|playlist|album)/([A-Za-z0-9]+)")


class Track:
    def __init__(self, title, duration, thumbnail, stream_url, requested_by):
        self.title = title
        self.duration = duration  # seconds
        self.thumbnail = thumbnail
        self.stream_url = stream_url
        self.requested_by = requested_by

    @property
    def duration_str(self) -> str:
        m, s = divmod(int(self.duration or 0), 60)
        return f"{m}:{s:02d}"


def _ytdlp_extract(query: str) -> dict:
    with yt_dlp.YoutubeDL(_YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            entries = [e for e in info["entries"] if e]
            if not entries:
                raise RuntimeError("No results found.")
            info = entries[0]
        return info


async def _extract(query: str) -> dict:
    return await asyncio.to_thread(_ytdlp_extract, query)


async def resolve(query: str, requested_by) -> list[Track]:
    """requested_by is stored as-is (a Pyrogram User) for display purposes."""
    m = _SPOTIFY_RE.search(query)
    if m:
        if not _spotify:
            raise RuntimeError("Spotify support isn't configured (missing SPOTIFY_CLIENT_ID/SECRET).")
        kind, item_id = m.groups()
        return await _resolve_spotify(kind, item_id, requested_by)

    search = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"
    info = await _extract(search)
    return [Track(
        title=info.get("title") or "Unknown title",
        duration=info.get("duration") or 0,
        thumbnail=info.get("thumbnail"),
        stream_url=info.get("url"),
        requested_by=requested_by,
    )]


async def _resolve_spotify(kind: str, item_id: str, requested_by) -> list[Track]:
    metas = []
    if kind == "track":
        metas = [await asyncio.to_thread(_spotify.track, item_id)]
    elif kind == "playlist":
        res = await asyncio.to_thread(_spotify.playlist_items, item_id, limit=50)
        metas = [it["track"] for it in res["items"] if it.get("track")]
    elif kind == "album":
        res = await asyncio.to_thread(_spotify.album_tracks, item_id, limit=50)
        metas = res["items"]

    tracks = []
    for meta in metas:
        name = meta.get("name", "")
        artists = ", ".join(a["name"] for a in meta.get("artists", []))
        query = f"ytsearch1:{artists} - {name} audio"
        try:
            info = await _extract(query)
        except Exception:
            continue
        tracks.append(Track(
            title=f"{artists} - {name}" if artists else name,
            duration=info.get("duration") or 0,
            thumbnail=info.get("thumbnail"),
            stream_url=info.get("url"),
            requested_by=requested_by,
        ))
    if not tracks:
        raise RuntimeError("Couldn't resolve any playable track from that Spotify link.")
    return tracks
      
