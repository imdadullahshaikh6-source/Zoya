# Music bot — setup on Oracle VPS

## 1. System packages
```bash
sudo apt update
sudo apt install -y ffmpeg python3-pip python3-venv
```

## 2. Python env
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. Credentials
- `API_ID` / `API_HASH` — from https://my.telegram.org (your own account, used to generate the assistant session).
- `BOT_TOKEN` — create a **new, separate** bot with @BotFather just for music. Do not reuse Zoya's token — two processes can't poll the same bot token at once.
- `STRING_SESSION` — generate once with Pyrogram on your own account (the "assistant"), e.g.:
  ```python
  from pyrogram import Client
  print(Client("gen", api_id=API_ID, api_hash=API_HASH).export_session_string())
  ```
  Log in when prompted; copy the printed string into `STRING_SESSION`. This assistant account is what actually joins the voice chat and streams audio — the bot account itself never joins calls directly.
- `OWNER_ID` — your Telegram numeric user id.
- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` — optional, from https://developer.spotify.com/dashboard. Without these, Spotify links won't resolve, but YouTube search/links still work fine.

Copy `.env.example` to `.env`, fill it in, then export the variables (or use a process manager that loads `.env`, e.g. pm2/systemd `EnvironmentFile=`).

## 4. Run
```bash
python3 main.py
```
Add both the bot and keep it running via `systemd` or `pm2` for restarts. The assistant account must be a member of every group you want music in — it auto-joins on the first `.play` in a group (via an invite link the bot exports), as long as the bot itself has invite-link permission there.

## Known rough edges (please test live and report back)
- `player.py`'s exact PyTgCalls method names (`play`, `pause`, `resume`, `leave_call`, `MediaStream`, `AudioQuality`) match PyTgCalls' 2.x API as documented at the time this was written — if your installed version differs, check `pip show pytgcalls` and its changelog; nothing else in the bot needs to change if so.
- The auto-advance-on-track-end handler (`_on_stream_end` in main.py) assumes the update object exposes `.chat_id` — verify this fires when a song ends naturally (as opposed to `.skip`, which is tested logic and always works).
- 
