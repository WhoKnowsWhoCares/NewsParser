"""Transliteration helpers for full-text search.

The FTS index stems Russian and English separately: a latin query term
never matches its cyrillic spelling in summaries (ASEAN vs АСЕАН —
different alphabets produce different tokens). We generate the
transliterated variants of the query and OR them into the tsquery, which
covers abbreviations and names written in either script.
"""

# latin -> cyrillic (applied greedily, longest first)
_LAT_TO_CYR = [
    ("sch", "щ"), ("sh", "ш"), ("ch", "ч"), ("th", "ф"), ("ts", "ц"),
    ("ya", "я"), ("yu", "ю"), ("zh", "ж"), ("kh", "х"),
    ("a", "а"), ("b", "б"), ("v", "в"), ("g", "г"), ("d", "д"),
    ("e", "е"), ("z", "з"), ("i", "и"), ("y", "й"), ("k", "к"),
    ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"),
    ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"), ("f", "ф"),
    ("h", "х"), ("c", "к"), ("w", "в"), ("x", "кс"), ("q", "к"),
]

_CYR_TO_LAT = [
    ("щ", "sch"), ("ж", "zh"), ("ч", "ch"), ("ш", "sh"), ("ц", "ts"),
    ("я", "ya"), ("ю", "yu"), ("х", "kh"), ("й", "y"),
    ("а", "a"), ("б", "b"), ("в", "v"), ("г", "g"), ("д", "d"),
    ("е", "e"), ("ё", "e"), ("з", "z"), ("и", "i"), ("к", "k"),
    ("л", "l"), ("м", "m"), ("н", "n"), ("о", "o"), ("п", "p"),
    ("р", "r"), ("с", "s"), ("т", "t"), ("у", "u"), ("ф", "f"),
    ("ь", ""), ("ы", "y"), ("э", "e"), ("ю", "yu"), ("я", "ya"),
]

_CYR_ALPHABET = set("абвгдежзийклмнопрстуфхцчшщъыьэюяё")
_LAT_ALPHABET = set("abcdefghijklmnopqrstuvwxyz")


def _is_cyrillic(text: str) -> bool:
    return bool(text) and any(ch.lower() in _CYR_ALPHABET for ch in text)


def _is_latin(text: str) -> bool:
    return bool(text) and any(ch.lower() in _LAT_ALPHABET for ch in text)


def _translit(text: str, table: list[tuple[str, str]]) -> str:
    out: list[str] = []
    i = 0
    low = text.lower()
    while i < len(low):
        for src, dst in table:
            if low.startswith(src, i):
                out.append(dst)
                i += len(src)
                break
        else:
            out.append(low[i])  # keep separators/spaces as-is
            i += 1
    return "".join(out)


def search_variants(query: str) -> list[str]:
    """Query transliterations: latin->cyrillic and cyrillic->latin.

    Returns the original query plus transliterated variants of its
    word-level tokens (mixed-script words like "«Инфляция» в США" keep
    only their single-script tokens transliterated).
    """
    variants: list[str] = [query]
    table = _CYR_TO_LAT if _is_cyrillic(query) else _LAT_TO_CYR
    if _is_cyrillic(query) and _is_latin(query):
        # mixed: per-token transliteration of matching-script tokens
        tokens = []
        for tok in query.split():
            if _is_cyrillic(tok):
                tokens.append(_translit(tok, _CYR_TO_LAT))
            elif _is_latin(tok):
                tokens.append(_translit(tok, _LAT_TO_CYR))
            else:
                tokens.append(tok)
        variants.append(" ".join(tokens))
        return list(dict.fromkeys(variants))
    t = _translit(query, table)
    if t and t != query:
        variants.append(t)
    return list(dict.fromkeys(variants))
