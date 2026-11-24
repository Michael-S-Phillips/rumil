"""Helpers for CSL-JSON records, the canonical metadata format inside Rumil.

Every library item stores one CSL-JSON object (https://citeproc-js.readthedocs.io/en/latest/csl-json/markup.html),
the same format Zotero, pandoc, and citeproc use, so nothing is lost converting between them.
"""

from __future__ import annotations

import re
from datetime import date

from .ids import normalize_doi
from .journals import journal_from_macro
from .textutil import collapse_ws, strip_markup

CSL_TYPES = [
    "article", "article-journal", "article-magazine", "article-newspaper", "bill", "book",
    "broadcast", "chapter", "classic", "collection", "dataset", "document", "entry",
    "entry-dictionary", "entry-encyclopedia", "event", "figure", "graphic", "hearing",
    "interview", "legal_case", "legislation", "manuscript", "map", "motion_picture",
    "musical_score", "pamphlet", "paper-conference", "patent", "performance", "periodical",
    "personal_communication", "post", "post-weblog", "regulation", "report", "review",
    "review-book", "software", "song", "speech", "standard", "thesis", "treaty", "webpage",
]

TYPE_LABELS = {
    "article-journal": "Journal article", "article": "Preprint", "book": "Book", "chapter": "Book chapter",
    "paper-conference": "Conference paper", "report": "Report", "thesis": "Thesis", "webpage": "Web page",
    "dataset": "Dataset", "software": "Software", "manuscript": "Manuscript", "speech": "Presentation",
    "map": "Map", "document": "Document", "article-magazine": "Magazine article",
    "article-newspaper": "Newspaper article", "patent": "Patent", "post-weblog": "Blog post",
    "entry-encyclopedia": "Encyclopedia entry", "entry-dictionary": "Dictionary entry", "standard": "Standard",
}

NAME_VARS = ["author", "editor", "container-author", "collection-editor", "translator", "director",
             "interviewer", "recipient", "reviewed-author", "composer", "illustrator", "contributor",
             "editorial-director", "original-author"]
DATE_VARS = ["issued", "accessed", "original-date", "event-date", "submitted", "available-date"]

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_SEASONS = {"spring": 21, "summer": 22, "autumn": 23, "fall": 23, "winter": 24}


# ---------------------------------------------------------------- dates
def make_date(year: int | None, month: int | None = None, day: int | None = None) -> dict | None:
    if not year:
        return None
    parts = [int(year)]
    if month:
        parts.append(int(month))
        if day:
            parts.append(int(day))
    return {"date-parts": [parts]}


def parse_date(s: str | None) -> dict | None:
    """Parse the date strings found in the wild into a CSL date object."""
    if s is None:
        return None
    if isinstance(s, dict):
        return s
    s = collapse_ws(str(s))
    if not s:
        return None
    m = re.match(r"^(\d{4})(?:[-/.](\d{1,2})(?:[-/.](\d{1,2}))?)?(?:[T ].*)?$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)
        mo_ok = 1 <= mo <= 12 or 21 <= mo <= 24
        return make_date(y, mo if mo_ok else None, d if 1 <= d <= 31 and mo_ok else None)
    m = re.match(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{4})$", s)  # US style 3/6/2023
    if m:
        return make_date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    low = s.lower()
    year_m = re.search(r"\b(1[5-9]\d\d|20\d\d|21\d\d)\b", low)
    if not year_m:
        return {"literal": s}
    y = int(year_m.group(1))
    mo = None
    for name, num in _MONTHS.items():
        if re.search(rf"\b{name}[a-z]*\.?\b", low):
            mo = num
            break
    if mo is None:
        for name, num in _SEASONS.items():
            if name in low:
                return {"date-parts": [[y]], "season": num - 20}
    d = None
    if mo:
        dm = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b(?!\d)", re.sub(r"\b\d{4}\b", "", low))
        if dm and 1 <= int(dm.group(1)) <= 31:
            d = int(dm.group(1))
    return make_date(y, mo, d)


def date_parts(item: dict, var: str = "issued") -> list[int] | None:
    d = item.get(var)
    if not isinstance(d, dict):
        return None
    dp = d.get("date-parts")
    if dp and dp[0] and dp[0][0] not in (None, "", 0):
        try:
            return [int(x) for x in dp[0] if x not in (None, "")]
        except (TypeError, ValueError):
            return None
    if d.get("raw") or d.get("literal"):
        pd = parse_date(d.get("raw") or d.get("literal"))
        if pd and pd.get("date-parts"):
            return pd["date-parts"][0]
    return None


def year_of(item: dict) -> int | None:
    dp = date_parts(item, "issued")
    return dp[0] if dp else None


def date_str(item: dict, var: str = "issued") -> str:
    dp = date_parts(item, var)
    if not dp:
        d = item.get(var)
        return d.get("literal", "") if isinstance(d, dict) else ""
    return "-".join(f"{p:04d}" if i == 0 else f"{p:02d}" for i, p in enumerate(dp))


def today_date() -> dict:
    t = date.today()
    return make_date(t.year, t.month, t.day)


# ---------------------------------------------------------------- names
_PARTICLES = {"de", "del", "della", "der", "di", "du", "la", "le", "van", "von", "da", "dos", "das", "ten", "ter", "op"}


_SUFFIX_RE = re.compile(r"(?:jr|sr|ii|iii|iv|2nd|3rd)\.?", re.I)


def is_name_suffix(tok: str | None) -> bool:
    """'Jr.', 'Sr', 'III', 'Iii' (a capitals fix gone wrong), '2nd'. A lone 'V' is not one: it is usually an initial."""
    return bool(tok) and bool(_SUFFIX_RE.fullmatch(tok.strip().strip(",")))


def norm_suffix(tok: str) -> str:
    t = tok.strip().strip(",").rstrip(".")
    low = t.lower()
    return {"jr": "Jr.", "sr": "Sr."}.get(low) or (t.upper() if low in ("ii", "iii", "iv") else t)


def parse_name(s: str) -> dict:
    """'Phillips, Michael S.' or 'Michael S. Phillips' -> CSL name object. Handles suffixes in every common
    position: 'McSween, Jr., Harry Y.' (BibTeX), 'McSween, Harry Y., Jr.', 'McSween Jr., H. Y.',
    'Harry Y. McSween Jr.', and 'Harry Y. McSween, Jr.'."""
    s = collapse_ws(strip_markup(s)).strip(" ,;")
    if not s:
        return {}
    if s.startswith("{") and s.endswith("}"):
        return {"literal": s[1:-1]}
    suffix = ""
    if "," in s:
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if len(parts) >= 3 and is_name_suffix(parts[1]):  # BibTeX 'von Last, Jr, First'
            fam, suffix, given = parts[0], parts[1], ", ".join(parts[2:])
        elif len(parts) >= 3 and is_name_suffix(parts[-1]):  # 'Last, First, Jr.'
            fam, given, suffix = parts[0], ", ".join(parts[1:-1]), parts[-1]
        elif len(parts) == 2 and is_name_suffix(parts[1]):  # 'Harry Y. McSween, Jr.'
            n = parse_name(parts[0])
            if n.get("family"):
                n["suffix"] = norm_suffix(parts[1])
            return n
        else:
            fam, given = parts[0], parts[1] if len(parts) > 1 else ""
            if len(parts) > 2:
                given = ", ".join(parts[1:])
        n = {"family": fam, "given": given}
        if suffix:
            n["suffix"] = norm_suffix(suffix)
        return _split_suffix({k: v for k, v in n.items() if v})
    toks = s.split(" ")
    if len(toks) > 2 and is_name_suffix(toks[-1]):  # 'Harry Y. McSween Jr.'
        suffix, toks = toks[-1], toks[:-1]
    if len(toks) == 1:
        n = {"family": toks[0]}
    else:
        # Keep lowercase particles with the family name: "Imke de Pater" -> family "de Pater".
        i = len(toks) - 1
        while i > 1 and toks[i - 1].lower() in _PARTICLES:
            i -= 1
        n = {"family": " ".join(toks[i:]), "given": " ".join(toks[:i])}
    if suffix:
        n["suffix"] = norm_suffix(suffix)
    return n


def _split_suffix(n: dict) -> dict:
    """Move a suffix out of the family or given name: 'McSween Jr.' / 'Harry Y., Jr.' / family 'Jr.' with given
    'Harry Y. McSween' / family 'Iii' with given 'Rossman P. Irwin' all become family + given + suffix."""
    if n.get("literal") or n.get("suffix"):
        return n
    fam, given = (n.get("family") or "").strip(), (n.get("given") or "").strip()
    gt = given.replace(",", " ").split()
    if is_name_suffix(fam) and len(gt) >= 2:  # the whole family name is the suffix: names were split wrongly
        return dict(n, family=gt[-1], given=" ".join(gt[:-1]), suffix=norm_suffix(fam))
    ft = fam.split()
    if len(ft) >= 2 and is_name_suffix(ft[-1]):
        return dict(n, family=" ".join(ft[:-1]).rstrip(","), suffix=norm_suffix(ft[-1]))
    if len(gt) >= 2 and is_name_suffix(gt[-1]):
        return dict(n, given=" ".join(gt[:-1]).rstrip(","), suffix=norm_suffix(gt[-1]))
    return n


def name_str(n: dict, initials: bool = True) -> str:
    if not n:
        return ""
    if n.get("literal"):
        return n["literal"]
    fam = " ".join(x for x in [n.get("non-dropping-particle"), n.get("family")] if x)
    given = n.get("given", "")
    if initials and given:
        given = initials_of(given)
    return f"{fam}, {given}".strip(", ") if given else fam


def initials_of(given: str) -> str:
    """'Michael S.' -> 'M. S.'; 'Jean-Pierre' -> 'J.-P.'"""
    out = []
    for tok in given.replace(".", ". ").split():
        if "-" in tok:
            out.append("-".join(p[0] + "." for p in tok.split("-") if p))
        elif tok:
            out.append(tok[0] + ".")
    return " ".join(out)


def is_etal_name(n: dict) -> bool:
    """True for placeholder creators that mean 'and others' ('others', 'et al.', family 'al' given 'et')."""
    if not n:
        return False
    lit = (n.get("literal") or "").strip().lower().rstrip(".")
    fam = (n.get("family") or "").strip().lower().rstrip(".")
    giv = (n.get("given") or "").strip().lower().rstrip(".")
    return lit in ("others", "et al", "and others") or fam in ("et al", "others") or (fam == "al" and giv == "et")


def family_of(n: dict) -> str:
    if not n:
        return ""
    return n.get("family") or n.get("literal") or ""


def creators(item: dict) -> list[dict]:
    for role in ("author", "editor", "container-author", "director", "contributor"):
        if item.get(role):
            return item[role]
    return []


def first_creator_family(item: dict) -> str:
    c = creators(item)
    return family_of(c[0]) if c else ""


def authors_short(item: dict) -> str:
    """'Phillips', 'Phillips & Moersch', 'Phillips et al.'"""
    c = creators(item)
    fams = [family_of(n) for n in c if family_of(n)]
    if not fams:
        return ""
    if len(fams) == 1:
        return fams[0]
    if len(fams) == 2:
        return f"{fams[0]} & {fams[1]}"
    return f"{fams[0]} et al."


def authors_text(item: dict, limit: int = 0) -> str:
    c = creators(item)
    names = [name_str(n) for n in c]
    if limit and len(names) > limit:
        return "; ".join(names[:limit]) + f"; … (+{len(names) - limit})"
    return "; ".join(names)


def short_ref(item: dict) -> str:
    y = year_of(item)
    a = authors_short(item) or (item.get("title") or "")[:40]
    return f"{a} ({y})" if y else f"{a} (n.d.)"


def container_of(item: dict) -> str:
    return item.get("container-title") or item.get("collection-title") or item.get("publisher") or ""


# ---------------------------------------------------------------- cleaning
def clean_item(csl: dict) -> dict:
    """Normalize a CSL-JSON record (from any source, including hand-written or messy JSON) into a new dict."""
    out: dict = {}
    for k, v in (csl or {}).items():
        if k in ("id", "citation-key"):
            continue
        if k in NAME_VARS:
            if isinstance(v, str):  # "Smith, J.; Jones, K." / "Smith, J. and Jones, K." / "Lunar and Planetary Institute"
                v = split_name_list(v)
            if isinstance(v, dict):
                v = [v]
            if isinstance(v, list):
                v = [n for n in (_clean_name(x) for x in v) if n]
            else:
                continue
            if v:
                out[k] = v
            continue
        if k in DATE_VARS:
            d = _clean_date(v)
            if d:
                out[k] = d
            continue
        if isinstance(v, list) and k not in ("ISBN", "ISSN", "categories", "keyword"):
            # Some sources give single-valued fields as lists (e.g. Crossref's "title": ["..."]).
            v = next((x for x in v if isinstance(x, (str, int, float)) and str(x).strip()), None)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = str(v)
        if isinstance(v, str):
            v = v.strip()
            if not v:
                continue
        elif isinstance(v, list):
            v = [x for x in v if x]
            if not v:
                continue
            if k in ("ISBN", "ISSN"):
                v = str(v[0])
        elif isinstance(v, dict):
            if not v:
                continue
        elif v is None or isinstance(v, bool):
            continue
        out[k] = v
    if out.get("type") not in CSL_TYPES:
        out["type"] = "document" if not out.get("type") else _guess_type(str(out["type"]))
    if out.get("DOI"):
        d = normalize_doi(out["DOI"])
        if d:
            out["DOI"] = d
        else:
            out.pop("DOI")
    if isinstance(out.get("title"), str):
        out["title"] = collapse_ws(out["title"])
        if out["title"].endswith(".") and not out["title"].endswith(("..", "et al.", " sp.", " spp.")):
            out["title"] = out["title"][:-1]
    elif "title" in out:
        out.pop("title")
    if isinstance(out.get("container-title"), str) and out["container-title"].startswith("\\"):
        full = journal_from_macro(out["container-title"])
        if full:
            out["container-title"] = full
    if out.get("page"):
        out["page"] = re.sub(r"\s*[\u2010-\u2015\u2212]+\s*|\s*--?\s*", "-", str(out["page"]))
    return out


def _clean_date(v) -> dict | None:
    """Accept CSL date objects (incl. 'raw' and nulls), strings, or bare years; return a valid CSL date or None."""
    if v is None or v == "" or v == []:
        return None
    if isinstance(v, (int, float)):
        return make_date(int(v)) if 1000 <= int(v) <= 2200 else None
    if isinstance(v, str):
        return parse_date(v)
    if isinstance(v, list):  # bare date-parts
        return _clean_date({"date-parts": v if v and isinstance(v[0], list) else [v]})
    if not isinstance(v, dict):
        return None
    dp = v.get("date-parts")
    if dp:
        parts = []
        for x in (dp[0] if isinstance(dp, list) and dp and isinstance(dp[0], list) else []):
            try:
                xi = int(str(x).strip())
            except (TypeError, ValueError):
                break
            parts.append(xi)
        parts = parts[:3]
        if parts and 1000 <= parts[0] <= 2200:
            if len(parts) >= 2 and not (1 <= parts[1] <= 12 or 21 <= parts[1] <= 24):
                parts = parts[:1]  # invalid month (21-24 are CSL seasons)
            if len(parts) == 3 and not 1 <= parts[2] <= 31:
                parts = parts[:2]
            out = {"date-parts": [parts]}
            if len(dp) > 1 and isinstance(dp[1], list):  # date ranges
                end = [int(x) for x in dp[1] if str(x).strip().isdigit()][:3]
                if end:
                    out["date-parts"].append(end)
            if v.get("season"):
                out["season"] = v["season"]
            if v.get("circa"):
                out["circa"] = v["circa"]
            return out
    for key in ("raw", "literal"):
        if v.get(key):
            pd = parse_date(str(v[key]))
            if pd:
                return pd
    return None


_ORG_WORDS = re.compile(r"\b(institute|university|universit[a\u00e4]t|laboratory|lab|survey|society|agency|nasa|esa|jaxa|"
                        r"team|consortium|group|association|council|department|center|centre|office|committee|"
                        r"administration|program|programme|project|collaboration|organi[sz]ation|corporation|inc|ltd|"
                        r"llc|foundation|academy|academies|museum|observatory|service|working|board|division|commission|bureau|"
                        r"ministry|facility)\b", re.I)


def name_from_string(s: str) -> dict:
    """One name from free text; organizations become literal names instead of fake people."""
    s = collapse_ws(s)
    if not s:
        return {}
    if "," in s:
        return parse_name(s)
    if _ORG_WORDS.search(s) or len(s.split()) > 4 or s.isupper():
        return {"literal": s}
    return parse_name(s)


def split_name_list(s: str) -> list[dict]:
    """'Smith, J.; Jones, K.' or 'Smith, J. and Jones, K.' -> names; 'Lunar and Planetary Institute' -> one name."""
    out = []
    for part in [p for p in re.split(r"\s*;\s*", s) if p.strip()]:
        pieces = re.split(r"\s+and\s+|\s*&\s*", part)
        if len(pieces) > 1 and all("," in p for p in pieces):
            out += [name_from_string(p) for p in pieces]
        else:
            out.append(name_from_string(part))
    return [n for n in out if n]


def _clean_name(n) -> dict | None:
    if isinstance(n, str):
        n = name_from_string(n)
    if not isinstance(n, dict):
        return None
    n = {k: collapse_ws(str(v)) for k, v in n.items() if v not in (None, "")}
    if not (n.get("family") or n.get("literal") or n.get("given")):
        return None
    if not n.get("family") and not n.get("literal") and n.get("given"):
        n = {"literal": n["given"]}
    n = _split_suffix(n)
    if n.get("suffix"):
        n["suffix"] = norm_suffix(n["suffix"]) if is_name_suffix(n["suffix"]) else n["suffix"]
    g = n.get("given", "")
    if is_name_suffix(g):
        pass  # never turn 'III' into 'I. I. I.'
    elif re.fullmatch(r"[A-Z]{2,3}", g):  # RIS/Web of Science initials without periods: "MS" -> "M. S."
        n["given"] = " ".join(c + "." for c in g)
    elif re.fullmatch(r"(?:[A-Z]\.){2,3}", g):  # "M.S." -> "M. S."
        n["given"] = " ".join(c + "." for c in g.replace(".", ""))
    return n


def _guess_type(t: str) -> str:
    t = t.lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "journal-article": "article-journal", "journalarticle": "article-journal", "article-journal": "article-journal",
        "proceedings-article": "paper-conference", "conference-paper": "paper-conference",
        "book-chapter": "chapter", "preprint": "article", "posted-content": "article",
        "dissertation": "thesis", "phdthesis": "thesis", "web": "webpage", "website": "webpage",
        "computer-program": "software", "data": "dataset",
    }
    return aliases.get(t, "document")


def summary(row: dict) -> dict:
    """Compact item description used in tool results and the UI list."""
    csl = row["csl"]
    return {
        "citekey": row["citekey"],
        "type": csl.get("type"),
        "title": strip_markup(csl.get("title", "")),
        "authors": authors_text(csl, limit=4),
        "year": year_of(csl),
        "container": container_of(csl),
        "DOI": csl.get("DOI"),
        "status": row.get("verify_status", "unchecked"),
    }
