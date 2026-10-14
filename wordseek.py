# wordseek.py
import random
import difflib
import time
import html
import os
import platform

from telegram import Update
from telegram.ext import (
    ContextTypes, CommandHandler, MessageHandler, filters
)
import words
import database as dbase

# ---------- Startup ----------
START_TIME = time.time()

# ---------- Env ----------
def _load_admins():
    raw = os.environ.get("ADMIN_USERS", "")
    ids = []
    for x in raw.split(","):
        x = x.strip()
        if x.isdigit():
            ids.append(int(x))
    return ids

ADMIN_USERS = _load_admins()
SUGGESTIONS_CHANNEL = os.environ.get("SUGGESTIONS_CHANNEL", "")

# ---------- Game state ----------
active_games = {}
chat_levels = {}
chat_wins = {}
last_played = {}

HELP_TXT = (
    "<blockquote>🔍 𝙒𝙤𝙧𝙙𝙎𝙚𝙚𝙠 🔍</blockquote>\n\n"
    "<i>🎯 The Ultimate Word Guessing Challenge</i>\n\n"
    "🎮 <b>How to Play:</b>\n"
    "• Start with <code>/new</code>, <code>/new4</code>, <code>/new5</code> or <code>/new6</code>\n"
    "• <code>/new</code> = default 5-letter game\n"
    "• Type your guess (just the word)\n"
    "• Feedback:\n"
    "   🟩 correct letter, correct spot\n"
    "   🟨 correct letter, wrong spot\n"
    "   🟥 letter not in the word\n"
    "• Max 30 guesses per game\n\n"
    "⚡ <b>Controls:</b>\n"
    "<code>/hint</code> — Reveal one letter (−5 pts)\n"
    "<code>/end</code> — End current game\n\n"
    "📊 <code>/leaderboard [today|week|month|year|all]</code>\n"
    "<code>/myscore</code> — personal score\n"
    "<code>/suggestword &lt;word&gt;</code> — suggest a new word\n"
)

COMMANDS = [
    ("new", "Start a default 5-letter WordSeek game"),
    ("new4", "Start a 4-letter WordSeek game"),
    ("new5", "Start a 5-letter WordSeek game"),
    ("new6", "Start a 6-letter WordSeek game"),
    ("hint", "Get a hint for the current game"),
    ("leaderboard", "Show leaderboard (time filter supported)"),
    ("myscore", "View your personal score"),
    ("suggestword", "Suggest a new word"),
    ("end", "End the current WordSeek game"),
    ("stats", "(Admin only) Bot & VPS stats"),
]

# ---------- Utils ----------
def to_math_bold(text):
    res = []
    for ch in text:
        if 'A' <= ch <= 'Z': res.append(chr(ord(ch) - 65 + 0x1D5D4))
        elif 'a' <= ch <= 'z': res.append(chr(ord(ch) - 97 + 0x1D5EE))
        elif '0' <= ch <= '9': res.append(chr(ord(ch) - 48 + 0x1D7EC))
        else: res.append(ch)
    return "".join(res)

def get_guess_result(guess, target):
    result = [None] * len(guess)
    target_list = list(target)
    guess_list = list(guess)

    for i in range(len(guess)):
        if guess_list[i] == target_list[i]:
            result[i] = '🟩'
            target_list[i] = None

    for i in range(len(guess)):
        if result[i] is None:
            if guess_list[i] in target_list:
                result[i] = '🟨'
                target_list[target_list.index(guess_list[i])] = None
            else:
                result[i] = '🟥'

    return ''.join(result)

def is_valid_guess(word, length):
    if length == 4: dictionary = words.WORDS_4
    elif length == 5: dictionary = words.WORDS_5
    elif length == 6: dictionary = words.WORDS_6
    else: return False

    if word in dictionary: return True
    matches = difflib.get_close_matches(word, dictionary, n=1, cutoff=0.7)
    return bool(matches)

# 👇 WORdSEEK ORIGINAL SCORING SYSTEM (Hush hata diya)
def calculate_points(length, time_taken, hints_taken):
    base = 30 if length == 4 else (50 if length == 5 else 80)
    time_penalty = int(time_taken // 15)   # 15 sec = 1 pt
    hint_penalty = hints_taken * 5
    final_points = base - time_penalty - hint_penalty
    return max(10, min(base, final_points))

def get_level_word_list(level, length):
    if length == 4:
        if level == 1: return words.EASY_WORDS_4
        elif level == 2: return words.MEDIUM_WORDS_4
        else: return words.HARD_WORDS_4 + words.MEDIUM_WORDS_4
    elif length == 5:
        if level == 1: return words.EASY_WORDS_5
        elif level == 2: return words.MEDIUM_WORDS_5
        else: return words.HARD_WORDS_5 + words.MEDIUM_WORDS_5
    elif length == 6:
        if level == 1: return words.EASY_WORDS_6
        elif level == 2: return words.MEDIUM_WORDS_6
        else: return words.HARD_WORDS_6 + words.MEDIUM_WORDS_6
    return []

def update_chat_level(chat_id):
    wins = chat_wins.get(chat_id, 0)
    if wins < 3:
        chat_levels[chat_id] = 1
    elif wins < 7:
        chat_levels[chat_id] = 2
    else:
        chat_levels[chat_id] = 3

async def get_word_for_game(chat_id, length):
    if time.time() - last_played.get(chat_id, 0) > 3600:
        chat_levels[chat_id] = 1
        chat_wins[chat_id] = 0

    update_chat_level(chat_id)
    level = chat_levels.get(chat_id, 1)
    word_list = get_level_word_list(level, length)

    if not word_list:
        if length == 4: word_list = words.EASY_WORDS_4
        elif length == 5: word_list = words.EASY_WORDS_5
        else: word_list = words.EASY_WORDS_6

    history = await dbase.wordseek_get_history(chat_id)
    available = [w for w in word_list if w not in history]

    if len(available) < 10:
        keep_recent = history[-5:] if len(history) >= 5 else history
        await dbase.wordseek_reset_history(chat_id, keep_recent)
        available = [w for w in word_list if w not in keep_recent]
        if not available:
            available = word_list

    word = random.choice(available)
    await dbase.wordseek_add_history(chat_id, word)
    return word

# ---------- Game flow ----------
async def new_game(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_games:
        await ctx.bot.send_message(chat_id, "⚠️ A game is already active! Use /end to stop it.")
        return

    text = update.message.text
    command = text.split('@')[0].lower().lstrip('./')

    # 👇 /new or .new → default 5 letters
    if command == "new":
        length = 5
    elif command.endswith('4'):
        length = 4
    elif command.endswith('5'):
        length = 5
    elif command.endswith('6'):
        length = 6
    else:
        return

    word = await get_word_for_game(chat_id, length)

    active_games[chat_id] = {
        'word': word,
        'length': length,
        'guesses': [],
        'max_guesses': 30,
        'status': 'active',
        'hints_taken': 0,
        'start_time': time.time(),
    }

    board_text = f"<blockquote><b>Game started! Guess the {length}-letter word!</b></blockquote>\n\n"
    board_text += f"<b>{length}-letter mode · 0/30</b>"

    await ctx.bot.send_message(chat_id, board_text, parse_mode='HTML')

async def guess_word(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in active_games: return
    game = active_games[chat_id]
    if game['status'] != 'active': return
    if update.effective_user.is_bot: return

    text = update.message.text.strip().lower()
    if len(text) != game['length']: return

    if text in game['guesses']:
        await ctx.bot.send_message(chat_id, "⚠️ <b>Already guessed this word. Try another!</b>", parse_mode='HTML')
        return

    if not is_valid_guess(text, game['length']):
        await ctx.bot.send_message(chat_id, f"❌ <b>{text}</b> is not a valid {game['length']}-letter word.", parse_mode='HTML')
        return

    game['guesses'].append(text)
    guess_count = len(game['guesses'])

    board_lines = []
    for g in game['guesses']:
        result = get_guess_result(g, game['word'])
        spaced_result = ' '.join(result)
        board_lines.append(f"{spaced_result}  {to_math_bold(g.upper())}")

    board_text = f"<b>{game['length']}-letter mode · {guess_count}/30</b>\n\n" + "\n".join(board_lines)

    if text == game['word']:
        game['status'] = 'ended'
        user_id = update.effective_user.id
        user_name = update.effective_user.first_name

        # 👇 WordSeek original scoring (time + hints)
        time_taken = time.time() - game.get('start_time', time.time())
        points = calculate_points(game['length'], time_taken, game['hints_taken'])

        await dbase.wordseek_add_score(chat_id, user_id, user_name, points)

        chat_wins[chat_id] = chat_wins.get(chat_id, 0) + 1
        update_chat_level(chat_id)
        last_played[chat_id] = time.time()

        reactions = ["👍", "🔥", "🎉", "❤", "🥰", "👏", "🤩", "💯"]
        try: await update.message.set_reaction(random.choice(reactions))
        except Exception: pass

        win_text = (
            f"🎉 <b>Congrats! You guessed it correctly.</b>\n"
            f"<b>Correct Word:</b> {text}\n"
            f"<b>Added {points} to the leaderboard.</b>"
        )

        await ctx.bot.send_message(
            chat_id,
            f"<blockquote>{win_text}</blockquote>\n\n"
            f"Start with /new{game['length']}",
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id
        )
        del active_games[chat_id]
        return

    if guess_count >= 30:
        game['status'] = 'ended'
        last_played[chat_id] = time.time()
        over_text = "😔 <b>Game over! No one guessed the word.</b>"
        await ctx.bot.send_message(
            chat_id,
            f"<blockquote>{over_text}</blockquote>\n\n"
            f"Start with /new{game['length']}",
            parse_mode='HTML'
        )
        del active_games[chat_id]
        return

    await ctx.bot.send_message(chat_id, board_text, parse_mode='HTML')

# ---------- Hint ----------
async def hint_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in active_games:
        await ctx.bot.send_message(chat_id, "❌ No active game in this chat!"); return
    game = active_games[chat_id]
    if game['status'] != 'active': return

    word, length = game['word'], game['length']
    revealed_indices = set()
    for guess in game['guesses']:
        for i in range(length):
            if guess[i] == word[i]: revealed_indices.add(i)

    unrevealed = [i for i in range(length) if i not in revealed_indices]
    if not unrevealed:
        await ctx.bot.send_message(chat_id, "🤔 You already know all the letters!"); return

    hint_index = random.choice(unrevealed)
    hint_letter = word[hint_index].upper()
    game['hints_taken'] = game.get('hints_taken', 0) + 1

    await ctx.bot.send_message(
        chat_id,
        f"💡 <b>Hint:</b> Letter at position <b>{hint_index + 1}</b> is <b>{hint_letter}</b>.\n"
        f"<i>(This will cost you 5 points if you win!)</i>",
        parse_mode='HTML'
    )

# ---------- Leaderboard ----------
async def leaderboard_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = ctx.args or []
    time_key = "all"
    if args and args[0].lower() in ("today", "week", "month", "year", "all"):
        time_key = args[0].lower()

    scores = await dbase.wordseek_get_leaderboard(chat_id, time_key)

    if not scores:
        await ctx.bot.send_message(chat_id, f"🏆 No scores yet ({time_key})! Start with /new."); return

    label = {
        "today": "Today", "week": "This Week", "month": "This Month",
        "year": "This Year", "all": "All Time"
    }[time_key]

    text = f"<b>🏆 𝙒𝙤𝙧𝙙𝙎𝙚𝙚𝙠 𝙇𝙚𝙖𝙙𝙚𝙧𝙗𝙤𝙖𝙧𝙙</b>\n"
    text += f"<i>({label})</i>\n\n"
    for i, doc in enumerate(scores, 1):
        name = html.escape(str(doc.get("name", "Unknown")))
        score = doc.get("score", 0)
        medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else f"{i}."))
        text += f"{medal} <b>{name}</b> — {score} pts\n"

    await ctx.bot.send_message(chat_id, f"<blockquote>{text}</blockquote>", parse_mode='HTML')

# ---------- Myscore ----------
async def myscore_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    user_name = html.escape(update.effective_user.first_name)

    score = await dbase.wordseek_get_user_score(chat_id, user_id)

    if score == 0:
        await ctx.bot.send_message(
            chat_id,
            f"📊 <b>{user_name}</b>, no score yet.\n<code>/new</code> to start!",
            parse_mode='HTML'
        )
        return

    score_text = (
        f"📊 <b>Your WordSeek Score</b>\n\n"
        f"👤 <b>Name:</b> {user_name}\n"
        f"🏆 <b>Score:</b> {score} pts\n"
        f"📍 <b>Group:</b> This chat"
    )
    await ctx.bot.send_message(chat_id, f"<blockquote>{score_text}</blockquote>", parse_mode='HTML')

# ---------- Suggest Word ----------
async def suggestword_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not ctx.args:
        await ctx.bot.send_message(
            update.effective_chat.id,
            "❌ Usage: <code>/suggestword &lt;word&gt;</code>",
            parse_mode='HTML'
        )
        return

    word = ctx.args[0].strip()
    user = update.effective_user

    if " " in word or not word.isalpha():
        await ctx.bot.send_message(
            update.effective_chat.id,
            "❌ Only single word, letters only.",
            parse_mode='HTML'
        )
        return

    normalized = word.lower()
    existing = set()
    for lst in (words.WORDS_4, words.WORDS_5, words.WORDS_6):
        for w in lst:
            for variant in w.split("/"):
                existing.add(variant.lower())

    if normalized in existing:
        await ctx.bot.send_message(
            update.effective_chat.id,
            "❌ This word already exists in our database."
        )
        return

    suggestion_text = (
        f"<blockquote>📝 <b>New Word Suggestion:</b> {html.escape(word)}\n\n"
        f"<b>By:</b> {html.escape(user.first_name)}{' ' + html.escape(user.last_name) if user.last_name else ''}\n"
        f"<b>Username:</b> {'@' + user.username if user.username else 'N/A'}\n"
        f"<b>User ID:</b> {user.id}</blockquote>"
    )

    sent = False
    if SUGGESTIONS_CHANNEL:
        try:
            await ctx.bot.send_message(SUGGESTIONS_CHANNEL, suggestion_text, parse_mode='HTML')
            sent = True
        except Exception as e:
            print("Suggestions channel error:", e)

    for admin_id in ADMIN_USERS:
        try:
            await ctx.bot.send_message(admin_id, suggestion_text, parse_mode='HTML')
            sent = True
        except Exception as e:
            print(f"Admin {admin_id} error:", e)

    if sent:
        await ctx.bot.send_message(update.effective_chat.id, "✅ Suggestion submitted!")
    else:
        await ctx.bot.send_message(update.effective_chat.id, "❌ Failed to submit. Try later.")

# ---------- Stats (admin only) ----------
def _format_bytes(b):
    if not b: return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.2f} {unit}"
        b /= 1024
    return f"{b:.2f} PB"

async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user: return
    if update.effective_user.id not in ADMIN_USERS:
        return

    uptime = time.time() - START_TIME
    hours = int(uptime // 3600)
    minutes = int((uptime % 3600) // 60)

    try:
        import psutil
        mem = psutil.virtual_memory()
        total_mem = mem.total
        used_mem = mem.used
        free_mem = mem.available
        cpu_count = psutil.cpu_count() or 1
        load_avg = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
        cpu_model = platform.processor() or "Unknown"
    except ImportError:
        total_mem = 0
        used_mem = 0
        free_mem = 0
        cpu_count = os.cpu_count() or 1
        load_avg = (0, 0, 0)
        cpu_model = platform.processor() or "Unknown"

    try:
        import resource
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        bot_rss = rss_kb * 1024
    except Exception:
        bot_rss = 0

    users_count = 0
    groups_count = 0
    try:
        users_count = await dbase.wordseek_get_users_count()
        groups_count = await dbase.wordseek_get_groups_count()
    except Exception:
        pass

    text = "🤖 <b>Bot Statistics</b>\n\n"

    text += "<blockquote>📊 <b>Bot Overview</b>\n"
    text += f"├ Users: {users_count}\n"
    text += f"├ Groups: {groups_count}\n"
    text += f"├ Uptime: {hours}h {minutes}m\n"
    text += f"└ PID: {os.getpid()}</blockquote>\n\n"

    text += "<blockquote>🧠 <b>Bot Memory</b>\n"
    text += f"├ RSS: {_format_bytes(bot_rss)}\n"
    text += "└ (approx from resource)</blockquote>\n\n"

    text += "<blockquote>💻 <b>VPS System</b>\n"
    text += f"├ OS: {platform.system()} {platform.release()}\n"
    text += f"├ Arch: {platform.machine()}\n"
    text += f"├ CPUs: {cpu_count}x {cpu_model.split()[0]}\n"
    text += f"└ Hostname: {platform.node()}</blockquote>\n\n"

    if total_mem:
        text += "<blockquote>💾 <b>VPS Memory</b>\n"
        text += f"├ Total: {_format_bytes(total_mem)}\n"
        text += f"├ Used: {_format_bytes(used_mem)} ({used_mem/total_mem*100:.1f}%)\n"
        text += f"└ Free: {_format_bytes(free_mem)} ({free_mem/total_mem*100:.1f}%)</blockquote>\n\n"

        text += "<blockquote>⚡ <b>VPS Load</b>\n"
        text += f"├ 1min: {load_avg[0]:.2f}\n"
        text += f"├ 5min: {load_avg[1]:.2f}\n"
        text += f"├ 15min: {load_avg[2]:.2f}\n"
        text += f"└ Cores: {cpu_count}</blockquote>\n\n"

        text += "<blockquote>📈 <b>Performance</b>\n"
        text += f"├ System Memory: {bot_rss/total_mem*100:.3f}%\n"
        text += f"└ Python: {platform.python_version()}</blockquote>"

    await ctx.bot.send_message(update.effective_chat.id, text, parse_mode='HTML')

# ---------- End ----------
async def end_game(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in active_games: return
    game = active_games[chat_id]
    length = game['length']
    del active_games[chat_id]
    last_played[chat_id] = time.time()

    end_text = "🛑 <b>Game ended. The word was hidden.</b>"
    await ctx.bot.send_message(
        chat_id,
        f"<blockquote>{end_text}</blockquote>\n\nStart with /new{length}",
        parse_mode='HTML')

# ---------- Register ----------
def register(app):
    # /new + /new4 + /new5 + /new6
    app.add_handler(CommandHandler(["new", "new4", "new5", "new6"], new_game))
    app.add_handler(MessageHandler(filters.Regex(r'^[./]new[456]?$'), new_game))

    app.add_handler(CommandHandler("hint", hint_cmd))
    app.add_handler(MessageHandler(filters.Regex(r'^[./]hint$'), hint_cmd))

    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(MessageHandler(filters.Regex(r'^[./]leaderboard'), leaderboard_cmd))

    app.add_handler(CommandHandler("myscore", myscore_cmd))
    app.add_handler(MessageHandler(filters.Regex(r'^[./]myscore$'), myscore_cmd))

    app.add_handler(CommandHandler("suggestword", suggestword_cmd))
    app.add_handler(MessageHandler(filters.Regex(r'^[./]suggestword'), suggestword_cmd))

    app.add_handler(CommandHandler("stats", stats_cmd))

    app.add_handler(CommandHandler("end", end_game))
    app.add_handler(MessageHandler(filters.Regex(r'^[./]end$'), end_game))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.Regex(r'^[./]') & filters.ChatType.GROUPS,
        guess_word))
