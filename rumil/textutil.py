"""Small text helpers: markup stripping, ASCII folding, and title/name matching."""

from __future__ import annotations

import html
import re
import unicodedata
from difflib import SequenceMatcher

_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9:._-]*(?:\s[^<>]*)?/?>")  # real tags only: keeps "grains <2 mm and >1 mm"
_WS_RE = re.compile(r"\s+")
_QUOTES = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", " ": " ",
})
_RETRACTED_PREFIX = re.compile(r"^\s*(retracted|withdrawn|retraction|expression of concern)\s*[:\-]\s*", re.I)


_INLINE_TAG_RE = re.compile(r"</?(?:i|b|em|strong|sub|sup|sc|span|u|small|a|font|mml:[a-z]+|jats:(?:italic|bold|sub|sup|sc))"
                            r"(?:\s[^<>]*)?/?>", re.I)


def normalize_punct(s: str | None) -> str:
    """Typographic quotes, dashes, and spaces to their ASCII forms ('Elkins\u2010Tanton' -> 'Elkins-Tanton')."""
    return (s or "").translate(_QUOTES)


def strip_markup(s: str | None) -> str:
    """Remove HTML/JATS/MathML tags and entities and collapse whitespace.

    Inline tags vanish without a space ('H<sub>2</sub>O' -> 'H2O'); block tags become a space."""
    if not s:
        return ""
    s = _INLINE_TAG_RE.sub("", str(s))
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


_LIGATURES = (("ß", "ss"), ("æ", "ae"), ("Æ", "AE"), ("œ", "oe"), ("Œ", "OE"), ("ø", "o"), ("Ø", "O"),
              ("ł", "l"), ("Ł", "L"), ("đ", "d"), ("Đ", "D"))


def ascii_fold(s: str | None) -> str:
    """ASCII approximation that keeps case ('Ångström' -> 'Angstrom')."""
    if not s:
        return ""
    s = str(s).translate(_QUOTES)
    for a, b in _LIGATURES:
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.encode("ascii", "ignore").decode()


def fold(s: str | None) -> str:
    """Lowercase ASCII approximation (é -> e, ß -> ss)."""
    return ascii_fold(s).lower()


def norm_title(s: str | None) -> str:
    s = strip_markup(s)
    s = _RETRACTED_PREFIX.sub("", s)
    s = fold(s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return _WS_RE.sub(" ", s).strip()


def title_words(s: str | None) -> list[str]:
    """Words of a title for an exact comparison that ignores only case, accents, punctuation, hyphen-vs-space,
    '&' vs 'and', inline markup, HTML entities, and a 'RETRACTED:' prefix. 'impact-melt' and 'impact melting' differ."""
    return norm_title(re.sub(r"\s*&\s*", " and ", strip_markup(s))).split()


# Tags that Gale/ProQuest/InfoTrac exports append to titles: '...spacecraft.(Author abstract)'.
_DB_TAG_RE = re.compile(
    r"\.?(?:\s*\((?:author abstract|abstract|reports?|research article|brief article|letter to the editor|clinical report|"
    r"technical report|essay|survey|editorial|book review|case study|viewpoint essay|interview|conference news)\))+\s*$",
    re.I)


def strip_database_tags(title: str | None) -> str:
    """'Images of ... spacecraft.(Author abstract)' -> 'Images of ... spacecraft'."""
    t = title or ""
    return _DB_TAG_RE.sub("", t).rstrip() if _DB_TAG_RE.search(t) else t


_STOPWORDS = {"a", "an", "the", "of", "on", "in", "and", "for", "from", "to", "at", "by", "with", "as", "its", "via",
              "into", "or", "is", "are"}


def _stem(w: str) -> str:
    w = w.lower()
    return w[:5] if len(w) > 5 else w.rstrip("s")


def split_running_head(title: str | None) -> tuple[str, str]:
    """Wiley/AGU records often end the title with the journal's running head: '...basaltic volcanism on Mars:
    RECENT WIDE-SPREAD VOLCANISM ON MARS' or '...observations of the Moon: Space weathering from LROC UV'.

    Returns (title without the running head, running head), or (title, '') when the last subtitle is a real one.
    A running head is short and either in capitals or made of words (and acronyms) from the main title."""
    t = title or ""
    i = t.rfind(": ")
    if i < 15:
        return t, ""
    main, head = t[:i].rstrip(), t[i + 2:].strip()
    plain_main, plain_head = strip_markup(main), strip_markup(head)
    words = plain_head.split()
    if not words or len(words) > 8 or len(plain_head) > 70 or len(plain_head) >= 0.8 * len(plain_main):
        return t, ""
    letters = [c for c in plain_head if c.isalpha()]
    main_letters = [c for c in plain_main if c.isalpha()]
    if len(words) >= 2 and letters and all(c.isupper() for c in letters) and not all(c.isupper() for c in main_letters):
        return main, head
    main_words = norm_title(plain_main).split()
    stems = {_stem(w) for w in main_words}
    initials = "".join(w[0] for w in main_words if w not in _STOPWORDS)
    content = [w for w in re.findall(r"[A-Za-z0-9]+", ascii_fold(plain_head)) if len(w) >= 3 and w.lower() not in _STOPWORDS]
    if len(content) < 2:
        return t, ""
    hits = sum(_stem(w) in stems or (w.isupper() and w.lower() in initials) for w in content)
    if hits >= 2 and hits / len(content) >= 0.6:
        return main, head
    return t, ""


_NUM_CONTEXT = {"paper", "part", "pt", "vol", "volume", "chapter", "ch", "series", "section", "phase", "no", "number",
                "stage", "sol", "issue", "experiment", "study", "report", "edition", "ed", "mission", "type", "class"}
_ROMAN = re.compile(r"^(?=[ivx])(x{0,3})(ix|iv|v?i{0,3})$")


def numeric_tokens(normalized_title: str) -> set[str]:
    """Numbers and roman numerals that distinguish otherwise identical titles ('Paper I' vs 'Paper II')."""
    toks = normalized_title.split()
    out = set()
    for i, t in enumerate(toks):
        if t.isdigit():
            out.add(t.lstrip("0") or "0")
        elif _ROMAN.match(t) and ((i > 0 and toks[i - 1] in _NUM_CONTEXT) or i == len(toks) - 1):
            out.add("roman:" + t)
    return out


def title_similarity(a: str | None, b: str | None) -> float:
    """0..1 similarity robust to case, punctuation, accents, and a missing subtitle.

    Titles that differ only in a number or roman numeral (Paper I / Paper II, Part 1 / Part 2) are
    capped at 0.8 so they are never treated as the same work."""
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    short, long_ = sorted((na, nb), key=len)
    # One title is the other without its subtitle ("Main title: subtitle").
    if long_.startswith(short) and len(short) >= 20 and len(short) / len(long_) >= 0.35:
        sim = 0.95
    else:
        ratio = SequenceMatcher(None, na, nb).ratio()
        ta, tb = set(na.split()), set(nb.split())
        jacc = len(ta & tb) / max(1, len(ta | tb))
        sim = max(ratio, jacc)
    if numeric_tokens(na) != numeric_tokens(nb):
        sim = min(sim, 0.8)
    return sim


def title_in_text(title: str | None, text: str | None) -> bool:
    """Does `text` (e.g. extracted from a PDF or web page) contain this title? Tolerates line breaks,
    hyphenation, accents, and punctuation."""
    if not title or not text:
        return False
    t = re.sub(r"[^a-z0-9]", "", fold(strip_markup(title)))
    body = re.sub(r"[^a-z0-9]", "", fold(text))
    if len(t) >= 12 and t in body:
        return True
    words = [w for w in norm_title(title).split() if len(w) > 3]
    flat = " " + re.sub(r"[^a-z0-9]+", " ", fold(text)) + " "
    return len(words) >= 3 and sum(f" {w} " in flat for w in words) / len(words) >= 0.9


def name_key(s: str | None) -> str:
    """Comparable form of a family name: 'Rivera-Valentín' -> 'riveravalentin'."""
    return re.sub(r"[^a-z]", "", fold(s))


_PARTICLES_RE = re.compile(r"^(?:(?:von|van|de|der|den|di|da|du|del|della|des|la|le|ten|ter|zu|dos|das|do|af|av)\s+)+")


def family_matches(a: str | None, b: str | None) -> bool:
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    # 'von Arx' vs 'Arx', 'van der Bogert' vs 'Bogert'
    pa, pb = name_key(_PARTICLES_RE.sub("", fold(a))), name_key(_PARTICLES_RE.sub("", fold(b)))
    if pa and pa == pb:
        return True
    # Particles and compound names: "de Pater" vs "Pater", "Viviano-Beck" vs "Viviano".
    return (len(ka) >= 4 and len(kb) >= 4) and (ka.endswith(kb) or kb.endswith(ka) or ka.startswith(kb) or kb.startswith(ka))


def collapse_ws(s: str | None) -> str:
    return _WS_RE.sub(" ", s or "").strip()


def truncate(s: str | None, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"
