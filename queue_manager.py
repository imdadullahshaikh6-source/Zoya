"""Per-chat playback queue, kept in memory. A restart drops the live voice-chat
stream anyway, so there is nothing meaningful to persist to Mongo here."""


class ChatQueue:
    def __init__(self):
        self.tracks = []       # list[Track] — index 0 is the one currently playing
        self.paused = False
        self.panel = None      # (chat_id, message_id) of the "Now Playing" card

    @property
    def current(self):
        return self.tracks[0] if self.tracks else None

    def add(self, track) -> int:
        self.tracks.append(track)
        return len(self.tracks)  # position; 1 means it's now playing

    def pop_current(self):
        if self.tracks:
            self.tracks.pop(0)
        self.paused = False
        return self.current

    def clear(self):
        self.tracks.clear()
        self.paused = False
        self.panel = None


_queues: dict[int, ChatQueue] = {}


def get(chat_id: int) -> ChatQueue:
    if chat_id not in _queues:
        _queues[chat_id] = ChatQueue()
    return _queues[chat_id]
  
