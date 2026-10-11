# wordseek.py
import random
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters
import words

# Game state
active_games = {}
user_scores = {}

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
    "• <code>/end</code> — End the current game"
)

COMMANDS = [
    ("new4", "Start a 4-letter WordSeek game"),
    ("new5", "Start a 5-letter WordSeek game"),
    ("new6", "Start a 6-letter WordSeek game"),
    ("end", "End the current WordSeek game"),
]

def get_guess_result(guess, target):
    result = [None] * len(guess)
    target_list = list(target)
    guess_list = list(guess)
    
    # First pass: find exact matches (Green)
    for i in range(len(guess)):
        if guess_list[i] == target_list[i]:
            result[i] = '🟩'
            target_list[i] = None
            
    # Second pass: find partial matches (Yellow/Orange) and wrong (Red)
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
    # Extract length from command (e.g., /new4 or .new4)
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
        'status': 'active'
    }
    
    # ✅ FIX 1: Initial board with spaces between squares
    squares = ' '.join(['🟥'] * length)
    question_marks = ' '.join(['?'] * length)
    
    board_text = f"<b>Game started! Guess the {length}-letter word!</b>\n\n"
    board_text += f"<b>{length}-letter mode · 0/30</b>\n"
    board_text += f"{squares}  {question_marks}"
    
    msg = await update.message.reply_text(board_text, parse_mode='HTML')
    active_games[chat_id]['message_id'] = msg.message_id

async def guess_word(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in active_games:
        return
    
    game = active_games[chat_id]
    if game['status'] != 'active':
        return
    
    # Ignore if message is from a bot
    if update.effective_user.is_bot:
        return
        
    text = update.message.text.strip().lower()
    
    # ✅ THINKING PART 1: Check length
    if len(text) != game['length']:
        return 
    
    # ✅ THINKING PART 2: Check if it's a valid word from our dictionary
    if text not in words.WORDS_4 and text not in words.WORDS_5 and text not in words.WORDS_6:
        await update.message.reply_text(
            f"❌ <b>{text}</b> is not a valid {game['length']}-letter word.", 
            parse_mode='HTML'
        )
        return 
    
    # If it passes the checks, register the guess
    game['guesses'].append(text)
    guess_count = len(game['guesses'])
    
    # Build the board
    board_lines = []
    for g in game['guesses']:
        result = get_guess_result(g, game['word'])
        # ✅ FIX 1 & 2: Add spaces between emojis and bold the word
        spaced_result = ' '.join(result)
        board_lines.append(f"{spaced_result}  <b>{g.upper()}</b>")
    
    board_text = f"<b>{game['length']}-letter mode · {guess_count}/30</b>\n\n"
    board_text += "\n".join(board_lines)
    
    # Check if won
    if text == game['word']:
        game['status'] = 'ended'
        user_id = update.effective_user.id
        user_scores[user_id] = user_scores.get(user_id, 0) + 20
        
        await update.message.reply_text(
            f"🎉 <b>Congrats! You guessed it correctly.</b>\n"
            f"<b>Correct Word:</b> {text}\n"
            f"<b>Added 20 to the leaderboard.</b>\n\n"
            f"Start with <code>/new{game['length']}</code>",
            parse_mode='HTML',
            reply_to_message_id=update.message.message_id
        )
        del active_games[chat_id]
        return
    
    # Check if max guesses reached
    if guess_count >= 30:
        game['status'] = 'ended'
        await update.message.reply_text(
            f"😔 <b>Game over! No one guessed the word.</b>\n"
            f"Start with <code>/new{game['length']}</code>",
            parse_mode='HTML'
        )
        del active_games[chat_id]
        return
    
    # Send updated board
    await update.message.reply_text(board_text, parse_mode='HTML')

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
    # Start commands
    app.add_handler(CommandHandler(["new4", "new5", "new6"], new_game))
    app.add_handler(MessageHandler(filters.Regex(r'^[./]new[456]$'), new_game))
    # End commands
    app.add_handler(CommandHandler("end", end_game))
    app.add_handler(MessageHandler(filters.Regex(r'^[./]end$'), end_game))
    # Guess handler (only for groups, text, not commands, not starting with . or /)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.Regex(r'^[./]') & filters.ChatType.GROUPS, 
        guess_word
    ))
