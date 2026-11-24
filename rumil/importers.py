"""Import BibTeX/BibLaTeX, RIS, and CSL-JSON files.

Citation keys from the file are kept. If a reference already exists in the
library under a different key, the file's key is added as an alias so documents
that use it keep compiling, and no duplicate is created.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .csl import clean_item, parse_date, parse_name
from .db import Library
from .ids import normalize_arxiv, normalize_doi
from .journals import journal_from_macro
from .textutil import collapse_ws

# --------------------------------------------------------------------- BibTeX
_BIB_TYPES = {
    "article": "article-journal", "book": "book", "inbook": "chapter", "incollection": "chapter",
    "inproceedings": "paper-conference", "conference": "paper-conference", "phdthesis": "thesis",
    "mastersthesis": "thesis", "thesis": "thesis", "techreport": "report", "report": "report",
    "unpublished": "manuscript", "online": "webpage", "electronic": "webpage", "www": "webpage",
    "dataset": "dataset", "software": "software", "manual": "report", "proceedings": "book",
    "booklet": "pamphlet", "patent": "patent", "inreference": "entry-encyclopedia", "collection": "book",
    "periodical": "periodical", "misc": "document",
}


def _delatex(s: str) -> str:
    """Convert common LaTeX markup in a field value to plain text with CSL inline HTML."""
    if not s:
        return ""
    try:
        from bibtexparser.latexenc import latex_to_unicode
        s = latex_to_unicode(s)
    except Exception:
        pass
    for cmd, tag in (("textit", "i"), ("emph", "i"), ("textbf", "b"), ("textsuperscript", "sup"),
                     ("textsubscript", "sub"), ("textsc", "sc")):
        s = re.sub(r"\\" + cmd + r"\s*\{([^{}]*)\}", rf"<{tag}>\1</{tag}>", s)
    s = re.sub(r"\$_\{?([^${}]+)\}?\$", r"<sub>\1</sub>", s)
    s = re.sub(r"\$\^\{?([^${}]+)\}?\$", r"<sup>\1</sup>", s)
    s = re.sub(r"\\(?:url|href)\{([^}]*)\}(?:\{[^}]*\})?", r"\1", s)
    s = s.replace(r"\&", "&").replace(r"\%", "%").replace(r"\_", "_").replace(r"\#", "#").replace(r"\$", "$")
    s = s.replace("~", "\u00a0").replace("---", "\u2014").replace("--", "\u2013")
    s = re.sub(r"\\[a-zA-Z]+\s*", "", s)  # drop remaining commands
    s = s.replace("{", "").replace("}", "").replace("$", "")
    return collapse_ws(s.replace("\u00a0", " "))


def _split_names(s: str) -> list[dict]:
    names, depth, cur = [], 0, ""
    tokens = re.split(r"(\{|\}|\s+and\s+)", s)
    for t in tokens:
        if t == "{":
            depth += 1
        elif t == "}":
            depth -= 1
        if re.fullmatch(r"\s+and\s+", t or "") and depth == 0:
            names.append(cur)
            cur = ""
        else:
            cur += t or ""
    if cur.strip():
        names.append(cur)
    out = []
    for raw in names:
        raw = raw.strip()
        if raw.lower() == "others":
            out.append({"literal": "others"})
            continue
        if raw.startswith("{") and raw.endswith("}") and raw.count("{") == 1:
            out.append({"literal": _delatex(raw[1:-1])})
            continue
        # Convert LaTeX accents before dropping braces: removing the braces in 'Val{\'e}rie' first leaves
        # "Val\'erie", which the converter reads as an accent on the wrong letter.
        n = parse_name(_delatex(raw))
        if n:
            out.append(n)
    return out


def parse_bibtex(text: str) -> list[tuple[str, dict, dict]]:
    """Return (citekey, csl, extra) triples. extra has 'tags' and 'notes'."""
    import bibtexparser
    from bibtexparser.bparser import BibTexParser

    parser = BibTexParser(common_strings=True, ignore_nonstandard_types=False, interpolate_strings=True)
    db = bibtexparser.loads(text, parser=parser)
    out = []
    for e in db.entries:
        f = {k.lower(): v for k, v in e.items()}
        et = f.pop("entrytype", "misc").lower()
        key = f.pop("id", "")
        ctype = _BIB_TYPES.get(et, "document")
        csl: dict[str, Any] = {"type": ctype}
        if f.get("title"):
            csl["title"] = _delatex(f["title"])
        if f.get("author"):
            csl["author"] = _split_names(f["author"])
        if f.get("editor"):
            csl["editor"] = _split_names(f["editor"])
        cont = f.get("journaltitle") or f.get("journal") or f.get("booktitle")
        if cont:
            csl["container-title"] = journal_from_macro(cont) or _delatex(cont)
        if f.get("date"):
            csl["issued"] = parse_date(f["date"])
        elif f.get("year"):
            month = f.get("month", "")
            csl["issued"] = parse_date(f"{_delatex(f['month'])} {f['year']}" if month and not month.isdigit()
                                       else (f"{f['year']}-{int(month):02d}" if month.isdigit() else f["year"]))
        for bk, ck in (("volume", "volume"), ("edition", "edition"), ("isbn", "ISBN"), ("issn", "ISSN"),
                       ("series", "collection-title"), ("abstract", "abstract"), ("note", "note"), ("type", "genre"),
                       ("version", "version")):
            if f.get(bk):
                csl[ck] = _delatex(f[bk])
        if f.get("number"):
            csl["number" if ctype in ("report",) else "issue"] = _delatex(f["number"])
        if f.get("issue"):
            csl["issue"] = _delatex(f["issue"])
        if f.get("pages"):
            csl["page"] = re.sub(r"-+", "-", _delatex(f["pages"]).replace("\u2013", "-"))
        pub = f.get("publisher") or f.get("school") or f.get("institution") or f.get("organization")
        if pub:
            csl["publisher"] = _delatex(pub)
        if f.get("address") or f.get("location"):
            csl["publisher-place"] = _delatex(f.get("address") or f.get("location"))
        if et == "mastersthesis":
            csl.setdefault("genre", "Master's thesis")
        elif et == "phdthesis":
            csl.setdefault("genre", "PhD thesis")
        if f.get("doi"):
            d = normalize_doi(f["doi"])
            if d:
                csl["DOI"] = d
        url = f.get("url") or (re.search(r"\\url\{([^}]+)\}", f.get("howpublished", "")) or [None, None])[1]
        if url:
            csl["URL"] = url.strip()
        if f.get("urldate"):
            csl["accessed"] = parse_date(f["urldate"])
        eprint = f.get("eprint")
        if eprint and (f.get("archiveprefix", "").lower() == "arxiv" or f.get("eprinttype", "").lower() == "arxiv"
                       or normalize_arxiv(eprint)):
            aid = normalize_arxiv(eprint)
            if aid:
                csl["number"] = f"arXiv:{aid}"
                if ctype in ("document", "webpage"):
                    csl["type"] = "article"
                    csl.setdefault("publisher", "arXiv")
                csl.setdefault("DOI", f"10.48550/arXiv.{aid}")
        if ctype == "document" and csl.get("URL") and not csl.get("container-title") and not csl.get("DOI"):
            csl["type"] = "webpage"
        extra = {"tags": [t.strip() for t in re.split(r"[,;]", f.get("keywords", "")) if t.strip()], "notes": []}
        out.append((key, clean_item(csl), extra))
    return out


# ----------------------------------------------------------------------- RIS
_RIS_TYPES = {"JOUR": "article-journal", "EJOUR": "article-journal", "JFULL": "article-journal", "BOOK": "book",
              "EBOOK": "book", "CHAP": "chapter", "ECHAP": "chapter", "CONF": "paper-conference",
              "CPAPER": "paper-conference", "ABST": "paper-conference", "THES": "thesis", "RPRT": "report",
              "ELEC": "webpage", "WEB": "webpage", "DATA": "dataset", "COMP": "software", "GEN": "document",
              "UNPB": "manuscript", "MAP": "map", "PAT": "patent", "MGZN": "article-magazine",
              "NEWS": "article-newspaper", "ENCYC": "entry-encyclopedia", "SLIDE": "speech", "STAND": "standard"}


def parse_ris(text: str) -> list[tuple[str, dict, dict]]:
    out = []
    rec: dict[str, list[str]] = {}
    last = None
    for line in text.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9])  -\s?(.*)$", line)
        if m:
            tag, val = m.group(1), m.group(2).strip()
            if tag == "ER":
                if rec:
                    out.append(_ris_to_csl(rec))
                rec, last = {}, None
                continue
            rec.setdefault(tag, []).append(val)
            last = tag
        elif last and line.strip():
            rec[last][-1] += " " + line.strip()
    if rec:
        out.append(_ris_to_csl(rec))
    return out


def _ris_to_csl(r: dict[str, list[str]]) -> tuple[str, dict, dict]:
    g = lambda *tags: next((r[t][0] for t in tags if r.get(t) and r[t][0]), None)  # noqa: E731
    ctype = _RIS_TYPES.get(g("TY") or "GEN", "document")
    csl: dict[str, Any] = {"type": ctype}
    if g("TI", "T1", "CT"):
        csl["title"] = g("TI", "T1", "CT")
    authors = [parse_name(a) for t in ("AU", "A1") for a in r.get(t, [])]
    if authors:
        csl["author"] = authors
    eds = [parse_name(a) for t in ("ED", "A2", "A3") for a in r.get(t, [])] if ctype in ("chapter", "book", "paper-conference") else []
    if eds:
        csl["editor"] = eds
    cont = g("JF", "JO", "T2", "BT", "JA", "J2") if ctype != "book" else None
    if cont:
        csl["container-title"] = cont
    date = g("DA", "PY", "Y1")
    if date:
        d = date.strip().rstrip("/")
        parts = [p for p in d.split("/") if p]
        csl["issued"] = parse_date("-".join(parts[:3]) if parts and parts[0].isdigit() else d)
    for tag, ck in (("VL", "volume"), ("IS", "issue"), ("PB", "publisher"), ("CY", "publisher-place"),
                    ("AB", "abstract"), ("N2", "abstract"), ("ET", "edition"), ("LA", "language")):
        if g(tag) and ck not in csl:
            csl[ck] = g(tag)
    sp, ep = g("SP"), g("EP")
    if sp:
        csl["page"] = f"{sp}-{ep}" if ep and ep != sp else sp
    if g("DO"):
        d = normalize_doi(g("DO"))
        if d:
            csl["DOI"] = d
    if g("UR"):
        csl["URL"] = g("UR")
    sn = g("SN")
    if sn:
        csl["ISBN" if ctype in ("book", "chapter") else "ISSN"] = sn
    notes = r.get("N1", [])
    extra = {"tags": r.get("KW", []), "notes": [n for n in notes if n]}
    return (g("ID") or "", clean_item(csl), extra)


# ------------------------------------------------------------------ CSL-JSON
def parse_csljson(text: str) -> list[tuple[str, dict, dict]]:
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("items") or [data]
    out = []
    for rec in data:
        key = rec.get("citation-key") or rec.get("id") or ""
        out.append((str(key) if key else "", clean_item(rec), {"tags": [], "notes": []}))
    return out


def parse_file(path: str | Path) -> list[tuple[str, dict, dict]]:
    p = Path(path).expanduser()
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    ext = p.suffix.lower()
    if ext in (".bib", ".bibtex", ".biblatex"):
        return parse_bibtex(text)
    if ext in (".ris", ".txt") and re.search(r"^TY  - ", text, re.M):
        return parse_ris(text)
    if ext in (".json", ".csljson"):
        return parse_csljson(text)
    if text.lstrip().startswith(("[", "{")):
        return parse_csljson(text)
    if re.search(r"^TY  - ", text, re.M):
        return parse_ris(text)
    return parse_bibtex(text)


# ------------------------------------------------------------------- import
_GENERIC_TYPES = {"document", "manuscript", "webpage", "dataset", "software", "report", "article"}


def _same_work_loose(lib: Library, csl: dict) -> list:
    """A .bib entry often types a work loosely (@misc for a DEM or a monograph) or dates it by print year rather
    than online year. Treat it as the library's item when the title is the same, the first author is the same, the
    years are within one, neither side has a conflicting DOI, and the types are equal or one of them is generic."""
    from .csl import creators, family_of, year_of
    from .textutil import family_matches, title_words
    au = creators(csl)
    if not csl.get("title") or not au:
        return []
    for d in lib.items_with_title(csl):
        dc = d["csl"]
        if title_words(dc.get("title")) != title_words(csl.get("title")):
            continue
        da = creators(dc)
        if not da or not family_matches(family_of(da[0]), family_of(au[0])):
            continue
        y1, y2 = year_of(dc), year_of(csl)
        if y1 and y2 and abs(y1 - y2) > 1:
            continue
        if dc.get("DOI") and csl.get("DOI") and dc["DOI"].lower() != csl["DOI"].lower():
            continue
        t1, t2 = dc.get("type"), csl.get("type")
        if t1 != t2 and t1 not in _GENERIC_TYPES and t2 not in _GENERIC_TYPES:
            continue
        return [(d, "same title and first author (entry type or year differs slightly)", 0.9)]
    return []


def import_records(lib: Library, records: list[tuple[str, dict, dict]], *, collection: str | None = None,
                   source: str = "file", dry_run: bool = False, actor: str = "user") -> dict:
    report: dict[str, Any] = {"added": [], "already_in_library": [], "aliases_added": [], "key_conflicts": [],
                              "possible_duplicates": [], "errors": []}
    for key, csl, extra in records:
        try:
            if not csl.get("title") and not csl.get("author"):
                report["errors"].append({"key": key, "error": "no title or author"})
                continue
            owner = lib.resolve_id(key, include_deleted=True) if key else None
            dups = lib.find_duplicates_of(csl, strong_only=True) or _same_work_loose(lib, csl)
            if dups:
                existing, why, _ = dups[0]
                report["already_in_library"].append({"file_key": key, "library_key": existing["citekey"], "why": why})
                if key and owner is not None and owner != existing["id"]:
                    other = lib.get(owner, full=False, include_deleted=True)
                    report["key_conflicts"].append({
                        "file_key": key, "matches": existing["citekey"],
                        "message": f"In this file '{key}' is '{(csl.get('title') or '')[:60]}' (in the library as "
                                   f"'{existing['citekey']}'), but in the library '{key}' names a different work "
                                   f"('{(other or {}).get('csl', {}).get('title', '')[:60]}'). Documents using this file "
                                   f"should cite '{existing['citekey']}' instead."})
                if not dry_run:
                    if key and owner is None and key.lower() != existing["citekey"].lower():
                        lib.add_alias(existing["id"], key)
                        report["aliases_added"].append({"alias": key, "for": existing["citekey"]})
                    if collection:
                        lib.add_to_collection(collection, [existing["id"]])
                continue
            if dry_run:
                report["added"].append({"citekey": key or "(new key)", "title": csl.get("title")})
                continue
            item = lib.add_item(csl, source=source, citekey=key if owner is None else None,
                                collections=[collection] if collection else [], tags=extra.get("tags", []), actor=actor)
            for n in extra.get("notes", []):
                lib.add_note(item["id"], f"<p>{n}</p>")
            report["added"].append({"citekey": item["citekey"], "title": csl.get("title")})
            if key and owner is not None:
                report["key_conflicts"].append({
                    "file_key": key, "imported_as": item["citekey"],
                    "message": f"'{key}' already names a different work in the library; this entry was imported as '{item['citekey']}'"})
            for d, why, _ in lib.find_duplicates_of(item["csl"], exclude_id=item["id"])[:1]:
                report["possible_duplicates"].append({"citekey": item["citekey"], "similar_to": d["citekey"], "why": why})
        except Exception as e:  # keep going; report the bad record
            report["errors"].append({"key": key, "error": f"{type(e).__name__}: {e}"})
    report["summary"] = (f"{len(report['added'])} added, {len(report['already_in_library'])} already in library, "
                         f"{len(report['aliases_added'])} aliases, {len(report['key_conflicts'])} key conflicts, "
                         f"{len(report['possible_duplicates'])} possible duplicates to review, {len(report['errors'])} errors")
    return report


def import_file(lib: Library, path: str | Path, collection: str | None = None, dry_run: bool = False) -> dict:
    recs = parse_file(path)
    rep = import_records(lib, recs, collection=collection, source=Path(path).suffix.lstrip(".") or "file", dry_run=dry_run)
    rep["file"] = str(path)
    return rep
