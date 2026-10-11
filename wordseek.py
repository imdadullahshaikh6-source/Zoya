# wordseek.py
import random
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters
import words

# Game state
active_games = {}
user_scores = {}
user_names = {}

HELP_TXT = (
    "<b>🔍 𝙒𝙤𝙧𝙙𝙎𝙚𝙚𝙠</b>\n\n"
    "Guess the hidden word! You have 30 attempts.\n\n"
    "<b>How to Play:</b>\n"
    "1. Start a game using <code>/new4</code>, <code>/new5</code>, or <code>/new6</code> (or with <code>.</code> like <code>.new4</code>).\n"
    "2. Guess the hidden word by simply typing it (no / or . needed).\n"
    "3. After each guess, you'll get color hints:\n"
    "   🟩 Correct letter in the right spot\n"
    "   🟨 Correct letter in the wrong spot\n"
    "   🟥 Letter not in the word\n"
    "4. First person to guess correctly wins!\n"
    "5. Maximum 30 guesses per game.\n\n"
    "<b>Commands:</b>\n"
    "• <code>/new4</code> — Start a 4-letter game\n"
    "• <code>/new5</code> — Start a 5-letter game\n"
    "• <code>/new6</code> — Start a 6-letter game\n"
    "• <code>/hint</code> — Get a hint (costs 5 points)\n"
    "• <code>/leaderboard</code> — See top players\n"
    "• <code>/end</code> — End the current game"
)

COMMANDS = [
    ("new4", "Start a 4-letter WordSeek game"),
    ("new5", "Start a 5-letter WordSeek game"),
    ("new6", "Start a 6-letter WordSeek game"),
    ("hint", "Get a hint for the current game"),
    ("leaderboard", "Show the WordSeek leaderboard"),
    ("end", "End the current WordSeek game"),
]

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

async def new_game(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_games:
        await update.message.reply_text("⚠️ A game is already active in this chat! Use /end to stop it.")
        return
    
    text = update.message.text
    command = text.split('@')[0].lower()
    
    if command.endswith('4'):
        length = 4
        words_list = words.WORDS_4
    elif command.endswith('5'):
        length = 5
        words_list = words.WORDS_5
    elif command.endswith('6'):
        length = 6
        words_list = words.WORDS_6
    else:
        return
    
    word = random.choice(words_list)
    active_games[chat_id] = {
        'word': word,
        'length': length,
        'guesses': [],
        'max_guesses': 30,
        'status': 'active',
        'hints_taken': 0
    }
    
    board_text = f"<b>Game started! Guess the {length}-letter word!</b>\n\n"
    board_text += f"<b>{length}-letter mode · 0/30</b>"
    
    msg = await update.message.reply_text(board_text, parse_mode='HTML')
    active_games[chat_id]['message_id'] = msg.message_id

async def guess_word(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in active_games:
        return
    
    game = active_games[chat_id]
    if game['status'] != 'active':
        return
    
    if update.effective_user.is_bot:
        return
        
    text = update.message.text.strip().lower()
    
    if len(text) != game['length']:
        return 
    
    if text in game['guesses']:
        await update.message.reply_text(
            f"⚠️ <b>Someone has already guessed this word. Please try another one!</b>", 
            parse_mode='HTML'
        )
        return

    if text not in words.WORDS_4 and text not in words.WORDS_5 and text not in words.WORDS_6:
        await update.message.reply_text(
            f"❌ <b>{text}</b> is not a valid {game['length']}-letter word.", 
            parse_mode='HTML'
        )
        return 
    
    game['guesses'].append(text)
    guess_count = len(game['guesses'])
    
    board_lines = []
    for g in game['guesses']:
        result = get_guess_result(g, game['word'])
        spaced_result = ' '.join(result)
        board_lines.append(f"{spaced_result}  <code><b>{g.upper()}</b></code>")
    
    board_text = f"<b>{game['length']}-letter mode · {guess_count}/30</b>\n\n"
    board_text += "\n".join(board_lines)
    
    if text == game['word']:
        game['status'] = 'ended'
        user_id = update.effective_user.id
        user_name = update.effective_user.first_name
        
        points = 20 - (game.get('hints_taken', 0) * 5)
        if points < 5: points = 5
        
        user_scores[user_id] = user_scores.get(user_id, 0) + points
        user_names[user_id] = user_name
        
        await update.message.reply_text(
            f"🎉 <b>Congrats! You guessed it correctly.</b>\n"
            f"<b>Correct Word:</b> {text}\n"
            f"<b>Added {points} to the leaderboard.</b>\n\n"
            f"Start with <code>/new{game['length']}</code>",
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id
        )
        del active_games[chat_id]
        return
    
    if guess_count >= 30:
        game['status'] = 'ended'
        await update.message.reply_text(
            f"😔 <b>Game over! No one guessed the word.</b>\n"
            f"Start with <code>/new{game['length']}</code>",
            parse_mode='HTML'
        )
        del active_games[chat_id]
        return
    
    await update.message.reply_text(board_text, parse_mode='HTML')

async def hint_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in active_games:
        await update.message.reply_text("❌ No active game in this chat!")
        return
    
    game = active_games[chat_id]
    if game['status'] != 'active':
        return
    
    word = game['word']
    length = game['length']
    
    revealed_indices = set()
    for guess in game['guesses']:
        for i in range(length):
            if guess[i] == word[i]:
                revealed_indices.add(i)
                
    unrevealed = [i for i in range(length) if i not in revealed_indices]
    
    if not unrevealed:
        await update.message.reply_text("🤔 You already know all the letters! Just guess the word.")
        return
        
    hint_index = random.choice(unrevealed)
    hint_letter = word[hint_index].upper()
    
    game['hints_taken'] = game.get('hints_taken', 0) + 1
    
    await update.message.reply_text(
        f"💡 <b>Hint:</b> The letter at position <b>{hint_index + 1}</b> is <b>{hint_letter}</b>.\n"
        f"<i>(This will cost you 5 points if you win!)</i>",
        parse_mode='HTML'
    )

async def leaderboard_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not user_scores:
        await update.message.reply_text("🏆 No scores yet! Start a game with /new5 to get on the board.")
        return
    
    sorted_scores = sorted(user_scores.items(), key=lambda x: x[1], reverse=True)
    
    text = "<b>🏆 𝙒𝙤𝙧𝙙𝙎𝙚𝙚𝙠 𝙇𝙚𝙖𝙙𝙚𝙧𝙗𝙤𝙖𝙧𝙙</b>\n\n"
    for i, (user_id, score) in enumerate(sorted_scores[:10], 1):
        name = user_names.get(user_id, "Unknown")
        text += f"{i}. <b>{name}</b> — {score} pts\n"
    
    await update.message.reply_text(text, parse_mode='HTML')

async def end_game(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in active_games:
        return
    game = active_games[chat_id]
    length = game['length']
    del active_games[chat_id]
    await update.message.reply_text(
        f"🛑 <b>Game ended by command. The word was hidden.</b>\n"
        f"Start with <code>/new{length}</code>",
        parse_mode='HTML'
    )

def register(app):
    app.add_handler(CommandHandler(["new4", "new5", "new6"], new_game))
    app.add_handler(MessageHandler(filters.Regex(r'^[./]new[456]$'), new_game))
    
    app.add_handler(CommandHandler("hint", hint_cmd))
    app.add_handler(MessageHandler(filters.Regex(r'^[./]hint$'), hint_cmd))
    
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(MessageHandler(filters.Regex(r'^[./]leaderboard$'), leaderboard_cmd))
    
    app.add_handler(CommandHandler("end", end_game))
    app.add_handler(MessageHandler(filters.Regex(r'^[./]end$'), end_game))
    
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.Regex(r'^[./]') & filters.ChatType.GROUPS, 
        guess_word
    ))
