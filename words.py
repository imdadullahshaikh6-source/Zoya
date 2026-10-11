# words.py
import os

def load_words_from_file(filename="words.txt"):
    # Get the absolute path relative to this file (words.py)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(base_dir, filename)
    
    if not os.path.exists(filepath):
        print(f"⚠️ WARNING: {filepath} NOT FOUND! Using fallback words.")
        return []
    
    with open(filepath, "r", encoding="utf-8") as f:
        all_words = [line.strip().lower() for line in f.readlines() if line.strip().isalpha()]
    
    print(f"✅ SUCCESS: Loaded {len(all_words)} words from {filename}")
    return all_words

# Load words
_all_words = load_words_from_file()

# Strictly filter by length
WORDS_4 = [w for w in _all_words if len(w) == 4]
WORDS_5 = [w for w in _all_words if len(w) == 5]
WORDS_6 = [w for w in _all_words if len(w) == 6]

# Fallback in case words.txt is missing (so bot doesn't crash)
if not WORDS_4:
    print("⚠️ Using hardcoded fallback words.")
    WORDS_4 = ["able", "acid", "aged", "also", "area", "army", "away", "baby", "back", "ball"]
    WORDS_5 = ["about", "above", "abuse", "actor", "acute", "admit", "adopt", "adult", "after", "again"]
    WORDS_6 = ["aboard", "accept", "access", "across", "acting", "action", "active", "actual", "advice", "advise"]
