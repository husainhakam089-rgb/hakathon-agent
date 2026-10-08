"""Arabic name normalization and fuzzy customer matching."""

import re

# Harakat (fatha, damma, kasra, tanween, shadda, sukun, ...) plus superscript alef.
_DIACRITICS = re.compile(r"[ً-ٰٟ]")
_TATWEEL = "ـ"

_CHAR_MAP = str.maketrans({
    "أ": "ا",
    "إ": "ا",
    "آ": "ا",
    "ٱ": "ا",
    "ة": "ه",
    "ى": "ي",
    # Arabic-Indic digits
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
    # Persian digits (common on some keyboards)
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
    "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
})

# Kunya prefixes that people write both joined and separated ("ابو علي" / "ابوعلي").
# We always join them so both spellings normalize to the same string.
_KUNYA_PREFIX = re.compile(r"(?:(?<=\s)|^)(ابو|ام)\s+")


def normalize(name: str) -> str:
    """Normalize an Arabic name for searching (not for display)."""
    if not name:
        return ""
    text = _DIACRITICS.sub("", name)
    text = text.replace(_TATWEEL, "")
    text = text.translate(_CHAR_MAP)
    text = text.lower()
    text = " ".join(text.split())
    text = _KUNYA_PREFIX.sub(r"\1", text)
    return text


def find_matches(query: str, customers):
    """Rank customers whose normalized name equals or contains the normalized query.

    `customers` is an iterable of dicts with at least `normalized_name`.
    Exact matches come first, then partial matches (shorter names first).
    """
    q = normalize(query)
    if not q:
        return []
    exact, partial = [], []
    for c in customers:
        name = c["normalized_name"]
        if name == q:
            exact.append(c)
        elif q in name:
            partial.append(c)
    partial.sort(key=lambda c: len(c["normalized_name"]))
    return exact + partial
