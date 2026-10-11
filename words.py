# words.py
import os

def load_words_from_file(filename="words.txt"):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(base_dir, filename)
    
    if not os.path.exists(filepath):
        print(f"⚠️ WARNING: {filepath} NOT FOUND! Using fallback words.")
        return []
    
    with open(filepath, "r", encoding="utf-8") as f:
        all_words = [line.strip().lower() for line in f.readlines() if line.strip().isalpha()]
    
    print(f"✅ SUCCESS: Loaded {len(all_words)} words from {filename}")
    return all_words

_all_words = load_words_from_file()

# Strictly filter by length
WORDS_4 = [w for w in _all_words if len(w) == 4]
WORDS_5 = [w for w in _all_words if len(w) == 5]
WORDS_6 = [w for w in _all_words if len(w) == 6]

# ✅ Naya Filter: Sirf common words rakho (jo shuru ke 50,000 me aate hain)
# 3.7L wali list alphabetical hai, toh shuru ke words zyada common hote hain
if len(WORDS_4) > 5000: WORDS_4 = WORDS_4[:10000]  # Top 10,000 4-letter words
if len(WORDS_5) > 5000: WORDS_5 = WORDS_5[:10000]  # Top 10,000 5-letter words
if len(WORDS_6) > 5000: WORDS_6 = WORDS_6[:10000]  # Top 10,000 6-letter words

# Fallback in case words.txt is missing (so the bot doesn't crash)
if not WORDS_4:
    print("⚠️ Using hardcoded fallback words.")
    WORDS_4 = ["able", "acid", "aged", "also", "area", "army", "away", "baby", "back", "ball"]
    WORDS_5 = ["about", "above", "abuse", "actor", "acute", "admit", "adopt", "adult", "after", "again"]
    WORDS_6 = ["aboard", "accept", "access", "across", "acting", "action", "active", "actual", "advice", "advise"]

# Level system ke liye
EASY_WORDS_4 = WORDS_4[:500] if len(WORDS_4) > 500 else WORDS_4
EASY_WORDS_5 = WORDS_5[:500] if len(WORDS_5) > 500 else WORDS_5
EASY_WORDS_6 = WORDS_6[:500] if len(WORDS_6) > 500 else WORDS_6

MEDIUM_WORDS_4 = WORDS_4[:3000] if len(WORDS_4) > 3000 else WORDS_4
MEDIUM_WORDS_5 = WORDS_5[:3000] if len(WORDS_5) > 3000 else WORDS_5
MEDIUM_WORDS_6 = WORDS_6[:3000] if len(WORDS_6) > 3000 else WORDS_6
