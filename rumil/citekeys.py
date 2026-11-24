"""Citation key generation.

Keys follow the format Zotero and Better BibTeX use by default, so keys already in
your LaTeX documents keep working: first author's family name in lowercase, the
first three significant title words in TitleCase, then the year, with a/b/c added
on collision.  Example: phillipsSaltConstructsPaleoLake2023.
"""

from __future__ import annotations

import re

from .csl import creators, family_of, year_of
from .textutil import ascii_fold, fold, strip_markup

SKIP_WORDS = set("""
a ab aboard about above across after against al along amid among an and anti around as at before behind below
beneath beside besides between beyond but by d da das de del dell dello dei degli della dem den der des despite die
do down du during ein eine einem einen einer eines el en et except for from gli i il in inside into is l la las le les
like lo los near nor of off on onto or over past per plus round save since so some sur than the through to toward
towards un una unas under underneath une unlike uno unos until up upon versus via von while with within without yet zu zum
""".split())

KEY_RE = re.compile(r"^[A-Za-z0-9_:.\-+/]+$")


def _clean_word(w: str) -> str:
    w = ascii_fold(strip_markup(w))
    return re.sub(r"[^A-Za-z0-9]", "", w)


def base_key(csl: dict) -> str:
    c = creators(csl)
    auth = _clean_word(family_of(c[0])).lower() if c else ""
    if not auth:
        auth = "anon"
    words = []
    title = strip_markup(csl.get("title") or csl.get("title-short") or "")
    for raw in re.split(r"[\s/]+", title):
        if not raw or fold(raw).strip("'\"()[]") in SKIP_WORDS:
            continue
        w = _clean_word(raw)
        if not w:
            continue
        words.append(w[0].upper() + w[1:])
        if len(words) == 3:
            break
    y = year_of(csl)
    return f"{auth}{''.join(words)}{y if y else ''}"


def make_unique(key: str, exists) -> str:
    """Append a, b, ..., z, aa, ab ... until exists(key) is False."""
    if not exists(key):
        return key
    suffixes = [chr(c) for c in range(ord("a"), ord("z") + 1)]
    suffixes += [a + b for a in suffixes for b in suffixes]
    for s in suffixes:
        if not exists(key + s):
            return key + s
    raise ValueError("could not generate a unique citation key")


def valid_key(key: str) -> bool:
    return bool(key) and bool(KEY_RE.match(key))
