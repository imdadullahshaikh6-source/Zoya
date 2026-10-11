# words.py
import json
import os

def load_words_from_json(filename="words.json"):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(base_dir, filename)
    
    if not os.path.exists(filepath):
        print(f"⚠️ WARNING: {filepath} NOT FOUND!")
        return {}
    
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    cleaned = {}
    for level, words_list in data.items():
        clean = []
        for w in words_list:
            for p in w.lower().split("/"):
                p = p.strip()
                if p.isalpha():
                    clean.append(p)
        cleaned[level] = list(set(clean))
    return cleaned

_data = load_words_from_json()

def filt(word_list, length):
    return [w for w in word_list if len(w) == length]

# EASY (A1 + A2)
EASY_WORDS_4 = filt(_data.get("A1", []) + _data.get("A2", []), 4)
EASY_WORDS_5 = filt(_data.get("A1", []) + _data.get("A2", []), 5)
EASY_WORDS_6 = filt(_data.get("A1", []) + _data.get("A2", []), 6)

# MEDIUM (B1 + B2)
MEDIUM_WORDS_4 = filt(_data.get("B1", []) + _data.get("B2", []), 4)
MEDIUM_WORDS_5 = filt(_data.get("B1", []) + _data.get("B2", []), 5)
MEDIUM_WORDS_6 = filt(_data.get("B1", []) + _data.get("B2", []), 6)

# HARD (C1 + C2)
HARD_WORDS_4 = filt(_data.get("C1", []) + _data.get("C2", []), 4)
HARD_WORDS_5 = filt(_data.get("C1", []) + _data.get("C2", []), 5)
HARD_WORDS_6 = filt(_data.get("C1", []) + _data.get("C2", []), 6)

# Combined (fallback)
WORDS_4 = EASY_WORDS_4 + MEDIUM_WORDS_4 + HARD_WORDS_4
WORDS_5 = EASY_WORDS_5 + MEDIUM_WORDS_5 + HARD_WORDS_5
WORDS_6 = EASY_WORDS_6 + MEDIUM_WORDS_6 + HARD_WORDS_6

print(f"✅ words.json loaded → 4L: {len(WORDS_4)}, 5L: {len(WORDS_5)}, 6L: {len(WORDS_6)}")
