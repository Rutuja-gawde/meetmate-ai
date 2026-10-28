"""Small, predictable clean-ups applied to Whisper's words (no model involved):

1. romanize_devanagari(): Hindi/Marathi that Whisper wrote in Devanagari is turned into
   Roman letters ("Hinglish"), so the transcript reads in one script.
2. NameCorrector: words that are a near-miss of a participant's name ("Rituja", "Srisant")
   are changed to the exact spelling typed on the upload form.

Both only change a word when they are sure it needs it, and never add or remove words.
"""

import re
from difflib import SequenceMatcher

# ------------------------------------------------------------------ Devanagari

_VOWELS = {  # independent vowels
    "अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri",
    "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e",
}
_MATRAS = {  # vowel signs after a consonant
    "ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e",
}
_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "ळ": "l",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
}
_NUKTA = "़"
_NUKTA_FORMS = {"ड": "r", "ढ": "rh", "क": "q", "ख": "kh", "ग": "g", "ज": "z", "फ": "f"}
_VIRAMA = "्"
_DIGITS = {ord("०") + i: str(i) for i in range(10)}
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LABIALS = set("पफबभम")


def _word_to_syllables(word):
    """[(consonant_text, vowel_text, is_inherent)] ; consonant_text may be '' for a pure vowel.
    Conjuncts (क्ष, त्र...) keep their consonants joined in one syllable."""
    syllables = []
    i, n = 0, len(word)
    while i < n:
        ch = word[i]
        if ch in _VOWELS:
            syllables.append(["", _VOWELS[ch], False])
            i += 1
        elif ch in _CONSONANTS:
            cons = _CONSONANTS[ch]
            i += 1
            if i < n and word[i] == _NUKTA:
                cons = _NUKTA_FORMS.get(ch, cons)
                i += 1
            # conjunct: consonant + virama + consonant ...
            while i + 1 < n and word[i] == _VIRAMA and word[i + 1] in _CONSONANTS:
                nxt = word[i + 1]
                cons += _CONSONANTS[nxt]
                i += 2
                if i < n and word[i] == _NUKTA:
                    i += 1
            if i < n and word[i] == _VIRAMA:          # trailing virama: bare consonant
                syllables.append([cons, "", False])
                i += 1
            elif i < n and word[i] in _MATRAS:
                syllables.append([cons, _MATRAS[word[i]], False])
                i += 1
            else:
                syllables.append([cons, "a", True])   # inherent vowel
        elif ch in "ंँ":                                # anusvara / chandrabindu
            nasal = "n"
            if i + 1 < n and word[i + 1] in _LABIALS:
                nasal = "m"
            if syllables:
                syllables[-1][1] += ""  # keep the vowel, add the nasal after it
                syllables[-1].append(nasal)
            else:
                syllables.append(["", "", False, nasal])
            i += 1
        elif ch == "ः":
            if syllables:
                syllables[-1].append("h")
            i += 1
        else:
            i += 1
    return syllables


def _romanize_word(word):
    syl = _word_to_syllables(word)
    last = len(syl) - 1
    delete = set()
    # Schwa deletion (Hindi/Marathi speech drops many inherent 'a's): never in a one-syllable
    # word, always at the end of a word, and in the middle unless that would leave a cluster
    # (the next syllable is a deleted final one, or the previous syllable was just deleted).
    if last >= 1:
        if syl[last][2]:
            delete.add(last)
        for i in range(1, last):
            if syl[i][2] and not (i + 1 == last and last in delete) and (i - 1) not in delete:
                delete.add(i)
    out = []
    for i, s in enumerate(syl):
        vowel = "" if i in delete else s[1]
        out.append(s[0] + vowel + "".join(s[3:]))
    return "".join(out)


def romanize_devanagari(text):
    """Replaces every Devanagari word in text with its Roman-letter spelling."""
    if not text or not _DEVANAGARI.search(text):
        return text

    def convert(match):
        word = match.group(0)
        return _romanize_word(word) if _DEVANAGARI.search(word) else word

    text = re.sub(r"[ऀ-ॿ]+", convert, text)
    text = text.replace("।", ".").replace("॥", ".")
    return text.translate(_DIGITS)


# ----------------------------------------------------------------------- names

class NameCorrector:
    """Fixes near-miss spellings of the participants' names."""

    def __init__(self, participants, threshold=0.78):
        self.threshold = threshold
        self.names = {}
        for full in participants or []:
            for part in re.findall(r"[A-Za-z]{3,}", full):
                self.names[part.lower()] = part[:1].upper() + part[1:]
        self.enabled = bool(self.names)

    def fix(self, word):
        if not self.enabled:
            return word
        m = re.match(r"^([^A-Za-z]*)([A-Za-z]{4,})([^A-Za-z]*)$", word)
        if not m:
            return word
        lead, core, trail = m.groups()
        low = core.lower()
        if low in self.names:
            return lead + self.names[low] + trail
        best, best_score = None, 0.0
        for key, proper in self.names.items():
            if abs(len(key) - len(low)) > 2:
                continue
            score = SequenceMatcher(None, low, key).ratio()
            if score > best_score:
                best, best_score = proper, score
        if best and best_score >= self.threshold:
            return lead + best + trail
        return word


def clean_word(raw, corrector, romanize=True):
    """One Whisper word (it usually starts with a space): romanize, then fix names.
    The surrounding spaces are kept exactly as they were."""
    text = romanize_devanagari(raw) if romanize else raw
    m = re.match(r"^(\s*)(.*?)(\s*)$", text, re.S)
    lead, core, trail = m.groups()
    return lead + (corrector.fix(core) if core else core) + trail
