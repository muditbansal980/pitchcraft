"""Caption / title safety filter for brand memes and reference posts (Agent B).

Deliberately blunt: a caption that matches is dropped (the slot stays a
placeholder), never "cleaned up". Covers slurs, sexual terms and common
English/Hindi abuse, including simple leetspeak and spacing tricks.
"""

import re
import unicodedata

_BLOCKLIST = (
    # slurs (English)
    "nigger", "nigga", "faggot", "fag", "retard", "retarded", "tranny", "chink", "spic", "kike",
    "paki", "coon", "gook", "dyke",
    # slurs (Indian context)
    "katua", "katwa", "chamar", "bhangi", "chinki", "hijra", "chakka", "kafir", "jihadi", "sanghi",
    "mulle", "bhakt", "andhbhakt", "librandu",
    # sexual
    "porn", "porno", "nude", "nudes", "naked", "sex", "sexy", "horny", "boobs", "tits", "dick",
    "cock", "pussy", "penis", "vagina", "blowjob", "handjob", "orgasm", "cum", "milf", "onlyfans",
    "nsfw", "xxx", "rape", "rapist", "molest",
    # abuse (English)
    "fuck", "fucking", "fucker", "motherfucker", "bitch", "slut", "whore", "cunt", "bastard",
    "asshole", "shit",
    # abuse (Hindi / Hinglish)
    "madarchod", "maderchod", "behenchod", "bhenchod", "benchod", "bhosdi", "bhosdike",
    "bhosda", "bhosadi", "chutiya", "chutiye", "chootiya", "chotiya", "chut", "lund", "lauda", "loda", "lavda", "gaand",
    "gandu", "randi", "harami", "haramkhor", "kamina", "kutiya", "jhatu", "tatte", "chodu",
    # violence / self-harm
    "kill yourself", "kys", "suicide", "terrorist",
)

_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower().translate(_LEET)
    text = re.sub(r"[^a-z\s]", " ", text)
    return " ".join(text.split())


_PATTERN = re.compile(r"\b(" + "|".join(re.escape(w) for w in sorted(_BLOCKLIST, key=len, reverse=True)) + r")\b")


def is_safe_text(*texts: str | None) -> bool:
    """False if any text contains a blocklisted term (also with letters spaced/dotted out)."""
    for raw in texts:
        if not raw:
            continue
        norm = _normalize(raw)
        if _PATTERN.search(norm):
            return False
        # "f u c k" / "c.h.u.t.i.y.a": collapse runs of single letters.
        collapsed = re.sub(r"\b(?:[a-z] ){2,}[a-z]\b", lambda m: m.group(0).replace(" ", ""), norm)
        if collapsed != norm and _PATTERN.search(collapsed):
            return False
    return True
