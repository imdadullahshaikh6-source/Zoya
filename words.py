# words.py
import os

def load_words_from_file(filename):
    if not os.path.exists(filename):
        print(f"⚠️ Warning: {filename} not found! Using fallback words.")
        return []
    
    with open(filename, "r", encoding="utf-8") as f:
        all_words = [line.strip().lower() for line in f.readlines() if line.strip().isalpha()]
    
    return all_words

_all_words = load_words_from_file("words.txt")

# Strictly filter by length
WORDS_4 = [w for w in _all_words if len(w) == 4]
WORDS_5 = [w for w in _all_words if len(w) == 5]
WORDS_6 = [w for w in _all_words if len(w) == 6]

# Fallback in case words.txt is missing (so the bot doesn't crash)
if not WORDS_4:
    WORDS_4 = ["able", "acid", "aged", "also", "area", "army", "away", "baby", "back", "ball"]
    WORDS_5 = ["about", "above", "abuse", "actor", "acute", "admit", "adopt", "adult", "after", "again"]
    WORDS_6 = ["aboard", "accept", "access", "across", "acting", "action", "active", "actual", "advice", "advise"]
