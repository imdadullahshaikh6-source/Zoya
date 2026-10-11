# words.py
import json
import os

def load_words_from_json(filename="words.json"):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(base_dir, filename)
    
    if not os.path.exists(filepath):
        print(f"⚠️ WARNING: {filepath} NOT FOUND! Using fallback words.")
        return {"A1": [], "A2": [], "B1": [], "B2": [], "C1": [], "C2": []}
    
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    # JSON me words ko saaf karo (slash wale words ko alag karo, special characters hatao)
    cleaned_data = {}
    for level, words in data.items():
        clean_list = []
        for w in words:
            # Agar word me slash hai (jaise "airplane/aeroplane"), toh dono ko alag karo
            parts = w.lower().split("/")
            for p in parts:
                p = p.strip()
                # Sirf alphabetic words rakho (no numbers, no symbols)
                if p.isalpha():
                    clean_list.append(p)
        cleaned_data[level] = list(set(clean_list)) # Duplicates hatao
    
    return cleaned_data

# Load the data
_data = load_words_from_json()

# Filter by length function
def filter_by_length(word_list, length):
    return [w for w in word_list if len(w) == length]

# --- EASY LEVEL (A1 + A2) ---
EASY_WORDS_4 = filter_by_length(_data.get("A1", []) + _data.get("A2", []), 4)
EASY_WORDS_5 = filter_by_length(_data.get("A1", []) + _data.get("A2", []), 5)
EASY_WORDS_6 = filter_by_length(_data.get("A1", []) + _data.get("A2", []), 6)

# --- MEDIUM LEVEL (B1 + B2) ---
MEDIUM_WORDS_4 = filter_by_length(_data.get("B1", []) + _data.get("B2", []), 4)
MEDIUM_WORDS_5 = filter_by_length(_data.get("B1", []) + _data.get("B2", []), 5)
MEDIUM_WORDS_6 = filter_by_length(_data.get("B1", []) + _data.get("B2", []), 6)

# --- HARD LEVEL (C1 + C2) ---
HARD_WORDS_4 = filter_by_length(_data.get("C1", []) + _data.get("C2", []), 4)
HARD_WORDS_5 = filter_by_length(_data.get("C1", []) + _data.get("C2", []), 5)
HARD_WORDS_6 = filter_by_length(_data.get("C1", []) + _data.get("C2", []), 6)

# Combine all for fallback
WORDS_4 = EASY_WORDS_4 + MEDIUM_WORDS_4 + HARD_WORDS_4
WORDS_5 = EASY_WORDS_5 + MEDIUM_WORDS_5 + HARD_WORDS_5
WORDS_6 = EASY_WORDS_6 + MEDIUM_WORDS_6 + HARD_WORDS_6

print(f"✅ SUCCESS: Loaded words from words.json")
print(f"   4-Letter -> Easy: {len(EASY_WORDS_4)}, Medium: {len(MEDIUM_WORDS_4)}, Hard: {len(HARD_WORDS_4)}")
print(f"   5-Letter -> Easy: {len(EASY_WORDS_5)}, Medium: {len(MEDIUM_WORDS_5)}, Hard: {len(HARD_WORDS_5)}")
print(f"   6-Letter -> Easy: {len(EASY_WORDS_6)}, Medium: {len(MEDIUM_WORDS_6)}, Hard: {len(HARD_WORDS_6)}")

# Agar JSON fail ho jaye toh fallback (bot crash na ho)
if not WORDS_4:
    print("⚠️ Using hardcoded fallback words.")
    EASY_WORDS_4 = ["able", "acid", "aged", "also", "area", "army", "away", "baby", "back", "ball"]
    EASY_WORDS_5 = ["about", "above", "abuse", "actor", "acute", "admit", "adopt", "adult", "after", "again"]
    EASY_WORDS_6 = ["aboard", "accept", "access", "across", "acting", "action", "active", "actual", "advice", "advise"]
    MEDIUM_WORDS_4 = EASY_WORDS_4
    MEDIUM_WORDS_5 = EASY_WORDS_5
    MEDIUM_WORDS_6 = EASY_WORDS_6
    HARD_WORDS_4 = EASY_WORDS_4
    HARD_WORDS_5 = EASY_WORDS_5
    HARD_WORDS_6 = EASY_WORDS_6
