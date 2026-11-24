"""Format citations and bibliographies with CSL styles (via citeproc-py).

Adds what citeproc-py lacks for real manuscripts: dependent styles (e.g. Icarus ->
Elsevier Harvard), year-suffix disambiguation (Phillips et al., 2023a/2023b),
narrative citations ("Phillips et al. (2023)"), author suppression, and output as
plain text, HTML, or formatted "runs" (italic/bold/superscript) for Word.
"""

from __future__ import annotations

import html
import re
import threading
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .config import Config
from .csl import authors_short, clean_item, creators, family_of, is_etal_name, year_of
from .http import Http, HttpError
from .textutil import fold, strip_markup

PKG_STYLES = Path(__file__).parent / "styles"
CSL_NS = "{http://purl.org/net/xbiblio/csl}"
CSL_REPO = "https://raw.githubusercontent.com/citation-style-language/styles/master"

STYLE_ALIASES = {
    "agu": "american-geophysical-union", "jgr": "journal-of-geophysical-research", "jgr-planets": "journal-of-geophysical-research-planets",
    "grl": "geophysical-research-letters", "icarus": "icarus", "psj": "the-planetary-science-journal", "aas": "the-astrophysical-journal",
    "apj": "the-astrophysical-journal", "apa": "apa", "nature": "nature", "science": "science", "elsevier": "elsevier-harvard",
    "pss": "planetary-and-space-science", "epsl": "earth-and-planetary-science-letters", "gsa": "the-geological-society-of-america",
    "maps": "meteoritics-and-planetary-science", "mdpi": "multidisciplinary-digital-publishing-institute",
    "remote-sensing": "remote-sensing", "chicago": "chicago-author-date", "ieee": "ieee", "lpsc": "lpsc",
}


@dataclass
class StyleInfo:
    id: str
    title: str
    path: Path
    independent_path: Path
    locale: str | None
    citation_format: str  # author-date | numeric | note | label | author
    et_al_min: int = 3
    et_al_use_first: int = 1
    add_year_suffix: bool = False
    renders_year_suffix: bool = False
    has_bibliography: bool = True
    dependent: bool = False


@dataclass
class CiteRef:
    key: str
    locator: str | None = None
    label: str = "page"
    prefix: str = ""
    suffix: str = ""
    suppress_author: bool = False
    narrative: bool = False


@dataclass
class CitationGroup:
    refs: list[CiteRef]
    raw: str = ""
    narrative: bool = False


_LABELS = {"p": "page", "pp": "page", "page": "page", "pages": "page", "fig": "figure", "figs": "figure",
           "figure": "figure", "figures": "figure", "chap": "chapter", "ch": "chapter", "chapter": "chapter",
           "sec": "section", "section": "section", "vol": "volume", "volume": "volume", "para": "paragraph",
           "paragraph": "paragraph", "l": "line", "line": "line", "n": "note", "note": "note", "col": "column",
           "column": "column", "bk": "book", "book": "book", "pt": "part", "part": "part", "v": "verse", "no": "issue",
           "issue": "issue", "§": "section"}

# One cite inside [...] in pandoc syntax: "see -@key, p. 33 and elsewhere"
_CITE_RE = re.compile(r"^(?P<prefix>.*?)(?P<minus>-)?@(?P<key>[\w:.#$%&\-+?<>~/]+?)(?P<rest>(?:[,\s].*)?)$", re.S)


def parse_cite_string(s: str) -> CitationGroup:
    """Parse pandoc-style citation text: '[@a; see @b, p. 4]', '@a', '-@a', 'a; b'."""
    raw = s.strip()
    inner = raw
    narrative = False
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    elif inner.startswith("@") and ";" not in inner and " " not in inner.strip():
        narrative = True
    refs = []
    for part in [p for p in inner.split(";") if p.strip()]:
        part = part.strip()
        if "@" not in part:
            part = "@" + part  # allow plain keys
        m = _CITE_RE.match(part)
        if not m:
            continue
        key = m.group("key").rstrip(".,")
        raw_rest = m.group("rest") or ""
        had_comma = raw_rest.lstrip().startswith(",")
        rest = raw_rest.strip().lstrip(",").strip()
        ref = CiteRef(key=key, prefix=m.group("prefix").strip(), suppress_author=bool(m.group("minus")))
        if rest:
            lm = re.match(r"^(?P<label>[A-Za-z§]+)\.?\s*(?P<loc>[\divxlcIVXLC][\w\-–,\s]*?)(?P<suf>(?:\s+(?![\d\-–]).*)?)$", rest)
            if lm and lm.group("label").lower().rstrip(".") in _LABELS:
                ref.label = _LABELS[lm.group("label").lower().rstrip(".")]
                ref.locator = lm.group("loc").strip().rstrip(",")
                ref.suffix = lm.group("suf") or ""
            elif re.match(r"^[\d\-–,\s]+$", rest):
                ref.locator = rest.strip()
            else:
                ref.suffix = (", " if had_comma else " ") + rest
        refs.append(ref)
    return CitationGroup(refs=refs, raw=raw, narrative=narrative)


class StyleRegistry:
    def __init__(self, cfg: Config, http: Http | None = None):
        self.cfg = cfg
        self.http = http or Http(cfg.get("contact_email", ""))
        self._cache: dict[str, StyleInfo] = {}
        self._parsed: dict[tuple, Any] = {}
        self._lock = threading.Lock()

    def dirs(self) -> list[Path]:
        return [d for d in (self.cfg.styles_dir, PKG_STYLES) if d.exists()]

    def list(self) -> list[dict]:
        seen, out = set(), []
        for d in self.dirs():
            for p in sorted(d.glob("*.csl")):
                if p.stem in seen:
                    continue
                seen.add(p.stem)
                try:
                    info = self._read_info(p)
                except Exception:
                    continue
                out.append({"id": p.stem, "title": info["title"], "format": info.get("format") or "",
                            "dependent": bool(info.get("parent")), "source": "user" if d == self.cfg.styles_dir else "built-in"})
        return sorted(out, key=lambda s: s["title"].lower())

    @staticmethod
    def _read_info(path: Path) -> dict:
        root = ET.parse(path).getroot()
        info = root.find(f"{CSL_NS}info")
        title = info.findtext(f"{CSL_NS}title") if info is not None else path.stem
        parent = None
        fmt = None
        if info is not None:
            for link in info.findall(f"{CSL_NS}link"):
                if link.get("rel") == "independent-parent":
                    parent = link.get("href", "").rstrip("/").split("/")[-1]
            for cat in info.findall(f"{CSL_NS}category"):
                if cat.get("citation-format"):
                    fmt = cat.get("citation-format")
        return {"title": title or path.stem, "parent": parent, "format": fmt, "locale": root.get("default-locale"), "root": root}

    def _find_file(self, name: str) -> Path | None:
        for d in self.dirs():
            p = d / f"{name}.csl"
            if p.exists():
                return p
        # match by title
        low = fold(name)
        for d in self.dirs():
            for p in d.glob("*.csl"):
                try:
                    if fold(self._read_info(p)["title"]) == low:
                        return p
                except Exception:
                    continue
        return None

    def install(self, name: str) -> Path:
        """Download a style (and its parent, for dependent styles) from the official CSL repository."""
        name = STYLE_ALIASES.get(name.lower(), name).removesuffix(".csl")
        name = re.sub(r"^https?://www\.zotero\.org/styles/", "", name.strip())
        if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", name):
            raise LookupError(f"'{name}' is not a CSL style id (lowercase letters, digits, and hyphens)")
        self.cfg.styles_dir.mkdir(parents=True, exist_ok=True)
        for sub in ("", "dependent/"):
            try:
                resp = self.http.request(f"{CSL_REPO}/{sub}{name}.csl")
            except HttpError as e:
                raise LookupError(f"Could not download style '{name}': {e}") from e
            if resp.ok and b"<style" in resp.body:
                p = self.cfg.styles_dir / f"{name}.csl"
                p.write_bytes(resp.body)
                parent = self._read_info(p)["parent"]
                if parent and not self._find_file(parent):
                    self.install(parent)
                return p
        raise LookupError(f"No CSL style named '{name}' in the CSL repository (see https://www.zotero.org/styles)")

    def get(self, name: str | None) -> StyleInfo:
        name = (name or self.cfg.get("default_style") or "american-geophysical-union").strip()
        name = STYLE_ALIASES.get(name.lower(), name)
        if name.endswith(".csl") and Path(name).expanduser().exists():
            path = Path(name).expanduser()
        else:
            path = self._find_file(name.removesuffix(".csl"))
            if path is None:
                path = self.install(name)
        key = str(path)
        mtime = path.stat().st_mtime
        cached = self._cache.get(key)
        if cached and getattr(cached, "_mtime", None) == mtime:
            return cached
        info = self._read_info(path)
        ind_path, locale = path, info["locale"]
        if info["parent"]:
            ppath = self._find_file(info["parent"]) or self.install(info["parent"])
            pinfo = self._read_info(ppath)
            ind_path = ppath
            root = pinfo["root"]
            fmt = info["format"] or pinfo["format"]
        else:
            root = info["root"]
            fmt = info["format"]
        cit = root.find(f"{CSL_NS}citation")
        et_min, et_first = 3, 1
        if cit is not None:
            et_min = int(cit.get("et-al-min") or 0) or 0
            et_first = int(cit.get("et-al-use-first") or 0) or 0
            if not et_min:
                nm = cit.find(f".//{CSL_NS}name")
                if nm is not None and nm.get("et-al-min"):
                    et_min, et_first = int(nm.get("et-al-min")), int(nm.get("et-al-use-first") or 1)
            if not et_min:
                for macro in root.findall(f"{CSL_NS}macro"):
                    nm = macro.find(f".//{CSL_NS}name[@et-al-min]")
                    if nm is not None:
                        et_min, et_first = int(nm.get("et-al-min")), int(nm.get("et-al-use-first") or 1)
                        break
        raw_text = ind_path.read_text(encoding="utf-8", errors="replace")
        si = StyleInfo(
            id=path.stem, title=info["title"], path=path, independent_path=ind_path, locale=locale,
            citation_format=fmt or ("numeric" if 'citation-number' in raw_text else "author-date"),
            et_al_min=et_min or 3, et_al_use_first=et_first or 1,
            add_year_suffix=bool(cit is not None and cit.get("disambiguate-add-year-suffix") == "true"),
            renders_year_suffix='variable="year-suffix"' in raw_text,
            has_bibliography=root.find(f"{CSL_NS}bibliography") is not None, dependent=bool(info["parent"]),
        )
        si._mtime = mtime  # type: ignore[attr-defined]
        self._cache[key] = si
        return si

    def parsed(self, si: StyleInfo):
        from citeproc import CitationStylesStyle

        k = (str(si.independent_path), si.locale, si.independent_path.stat().st_mtime)
        with self._lock:
            if k not in self._parsed:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    try:
                        self._parsed[k] = CitationStylesStyle(str(si.independent_path), locale=si.locale, validate=False)
                    except Exception:
                        self._parsed[k] = CitationStylesStyle(str(si.independent_path), validate=False)
            return self._parsed[k]


# --------------------------------------------------------------------- runs
class _RunParser(HTMLParser):
    TAGS = {"i": "italic", "em": "italic", "b": "bold", "strong": "bold", "sup": "superscript", "sub": "subscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.runs: list[dict] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.TAGS:
            self.stack.append(self.TAGS[tag])
        elif tag == "span" and "small-caps" in (a.get("style") or ""):
            self.stack.append("smallcaps")
        else:
            self.stack.append("")

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if not data:
            return
        fmt = {k: True for k in self.stack if k}
        if self.runs and {k: v for k, v in self.runs[-1].items() if k != "text"} == fmt:
            self.runs[-1]["text"] += data
        else:
            self.runs.append({"text": data, **fmt})


def tidy_punctuation(h: str) -> str:
    """Collapse doubled punctuation that citeproc-py leaves behind ('et al..', ' ,')."""
    h = re.sub(r"(?<!\.)\.((?:</[^>]+>)*)\.(?!\.)", r".\1", h)
    h = re.sub(r"\s+([,;])", r"\1", h)
    h = re.sub(r",((?:</[^>]+>)*)\.", r".\1", h)
    h = re.sub(r"([?!])((?:</[^>]+>)*)\.(?!\w)", r"\1\2", h)  # 'planet?.' -> 'planet?'
    return h


def html_to_runs(s: str) -> list[dict]:
    p = _RunParser()
    p.feed(s)
    p.close()
    return p.runs


def html_to_text(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s))


# ---------------------------------------------------------------- formatter
_YEAR_RE = re.compile(r"\b(1[5-9]\d\d|20\d\d|21\d\d|n\.d\.|in press|forthcoming)([a-z]{0,2})\b")


def _escape_fields(v):
    if isinstance(v, str):
        return html.escape(v, quote=False)
    if isinstance(v, list):
        return [_escape_fields(x) for x in v]
    if isinstance(v, dict):
        return {k: (_escape_fields(x) if k != "date-parts" else x) for k, x in v.items()}
    return v


class Formatter:
    def __init__(self, lib, styles: StyleRegistry):
        self.lib = lib
        self.styles = styles

    def _prep(self, item: dict) -> dict:
        c = clean_item(item["csl"])
        for k in ("note", "abstract", "annote", "keyword", "source", "call-number", "archive_location", "archive"):
            c.pop(k, None)
        if c.get("event-title") and not c.get("event"):
            c["event"] = c.pop("event-title")
        for k in ("title", "container-title", "collection-title"):
            if c.get(k):
                c[k] = strip_markup(c[k])
        for role in ("author", "editor"):
            if c.get(role):
                c[role] = [n for n in c[role] if not is_etal_name(n)]
                if not c[role]:
                    c.pop(role)
        if c.get("DOI") and c.get("URL"):
            c.pop("URL")  # styles print the DOI; a second link is redundant
            c.pop("accessed", None)
        if c.get("type") == "article-journal":
            c.pop("publisher", None)  # journal publishers don't belong in journal-article references
            c.pop("publisher-place", None)
        for dv in ("issued", "accessed", "original-date", "event-date"):
            d = c.get(dv)
            if isinstance(d, dict) and "date-parts" in d:
                c[dv] = {"date-parts": d["date-parts"][:1]}  # citeproc-py: no seasons or ranges
        # citeproc-py does not escape field text in HTML output; escape it here so library data can never
        # inject markup into the library window or a Word file.
        c = _escape_fields(c)
        c["id"] = item["citekey"]
        return c

    def _disambiguate(self, si: StyleInfo, records: dict[str, dict]) -> None:
        """Assign a/b/c year suffixes to works that would otherwise cite identically."""
        if si.citation_format not in ("author-date", "author") and not si.add_year_suffix:
            return
        groups: dict[tuple, list[str]] = {}
        for key, c in records.items():
            names = [family_of(n) for n in creators(c)]
            if len(names) >= si.et_al_min:
                vis = tuple(fold(n) for n in names[: si.et_al_use_first]) + ("et al",)
            else:
                vis = tuple(fold(n) for n in names)
            y = year_of(c)
            if not y:
                continue
            groups.setdefault((vis, y), []).append(key)
        for (vis, y), keys in groups.items():
            if len(keys) < 2:
                continue
            keys.sort(key=lambda k: (fold(strip_markup(records[k].get("title", ""))), k))
            for i, k in enumerate(keys):
                suffix = chr(ord("a") + i) if i < 26 else "a" + chr(ord("a") + i - 26)
                if si.renders_year_suffix:
                    records[k]["year-suffix"] = suffix
                else:
                    records[k]["issued"] = {"literal": f"{y}{suffix}"}

    def render(self, groups: list[CitationGroup], style: str | None = None, *, output: str = "text",
               bibliography: bool = True, extra_bib_keys: list[str] | None = None) -> dict:
        from citeproc import Citation, CitationItem, CitationStylesBibliography, Locator, formatter
        from citeproc.source.json import CiteProcJSON

        si = self.styles.get(style)
        missing: list[str] = []
        records: dict[str, dict] = {}
        resolved: dict[str, str] = {}  # as written -> canonical key
        warnings_out: list[str] = []
        status_notes: dict[str, str] = {}
        for g in groups:
            for r in g.refs:
                self._resolve(r.key, records, resolved, missing, status_notes)
        for k in extra_bib_keys or []:
            self._resolve(k, records, resolved, missing, status_notes)
        self._disambiguate(si, records)
        style_obj = self.styles.parsed(si)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            src = CiteProcJSON(list(records.values()))
            bib = CitationStylesBibliography(style_obj, src, formatter.html)
            cp_cits = []
            for g in groups:
                items = []
                for r in g.refs:
                    canon = resolved.get(r.key)
                    if not canon:
                        continue
                    kw = {}
                    if r.locator:
                        kw["locator"] = Locator(r.label or "page", r.locator)
                    if r.prefix:
                        kw["prefix"] = r.prefix + " "
                    if r.suffix:
                        kw["suffix"] = r.suffix
                    items.append(CitationItem(canon, **kw))
                cit = Citation(items) if items else None
                cp_cits.append(cit)
                if cit:
                    bib.register(cit)
            for k in extra_bib_keys or []:
                canon = resolved.get(k)
                if canon:
                    bib.register(Citation([CitationItem(canon)]))
            if si.has_bibliography:
                bib.sort()  # orders the bibliography (and numbering for numeric styles) per the style
            out_cits = []
            for g, cit in zip(groups, cp_cits):
                if cit is None:
                    out_cits.append({"input": g.raw, "html": "", "text": f"[MISSING: {'; '.join(r.key for r in g.refs)}]",
                                     "missing": [r.key for r in g.refs]})
                    continue
                h = tidy_punctuation(str(bib.cite(cit, lambda ci: None)))
                h = self._post_cite(si, g, h, records, resolved)
                miss = [r.key for r in g.refs if r.key not in resolved]
                out_cits.append({"input": g.raw, "html": h, "text": html_to_text(h), "missing": miss,
                                 "keys": [resolved[r.key] for r in g.refs if r.key in resolved]})
            entries = []
            if bibliography and si.has_bibliography:
                lower_to_key = {rk.lower(): rk for rk in records}
                for it in bib.items:
                    rendered = bib.style.render_bibliography([it])
                    if not rendered:
                        continue
                    h = tidy_punctuation(str(rendered[0]))
                    if si.citation_format == "numeric":
                        h = re.sub(r"^((?:<[^>]+>)*\[?\d+\]?\.?(?:</[^>]+>)*)(?=[^\s.\]])", r"\1 ", h)
                    entries.append({"key": lower_to_key.get(it.key, it.key), "html": h, "text": html_to_text(h)})
        for k, note in status_notes.items():
            warnings_out.append(f"@{k}: {note}")
        result = {"style": {"id": si.id, "title": si.title, "format": si.citation_format}, "citations": out_cits,
                  "bibliography": entries, "missing": sorted(set(missing)), "warnings": warnings_out}
        if output == "runs":
            for c in out_cits:
                c["runs"] = html_to_runs(c["html"]) if c["html"] else [{"text": c["text"]}]
            for e in entries:
                e["runs"] = html_to_runs(e["html"])
        if output == "text":
            for c in out_cits:
                c.pop("html", None)
            for e in entries:
                e.pop("html", None)
        return result

    def _resolve(self, key, records, resolved, missing, status_notes) -> None:
        if key in resolved or key in missing:
            return
        item = self.lib.get(key, full=False)
        if not item:
            missing.append(key)
            return
        canon = item["citekey"]
        resolved[key] = canon
        if canon not in records:
            records[canon] = self._prep(item)
            st = item.get("verify_status")
            if st in ("retracted", "doi_invalid", "mismatch", "differs"):
                status_notes[canon] = {"retracted": "RETRACTED work", "doi_invalid": "DOI does not resolve",
                                       "mismatch": "the DOI's registry record looks like a different work",
                                       "differs": "title/authors/pages differ from the registry record"}[st]
            elif st in ("unverified", "unchecked", "doi_found", "url_dead"):
                status_notes[canon] = f"not verified ({st})"

    def _post_cite(self, si: StyleInfo, g: CitationGroup, h: str, records: dict, resolved: dict) -> str:
        single = len(g.refs) == 1
        ref = g.refs[0] if g.refs else None
        if not single or not ref:
            return h
        narrative = g.narrative or ref.narrative
        if si.citation_format in ("author-date", "author"):
            if ref.suppress_author or narrative:
                text = h
                m = re.match(r"^(\s*(?:<[^>]+>)*)([(\[])(.*)([)\]])((?:</[^>]+>)*\s*)$", text, re.S)
                if not m:
                    return h
                inner = m.group(3)
                ym = _YEAR_RE.search(html_to_text(inner))
                if not ym:
                    return h
                # Split on the first year occurrence in the inner HTML.
                idx = inner.find(ym.group(0))
                if idx < 0:
                    return h
                author_part = inner[:idx].rstrip().rstrip(",").rstrip()
                rest = inner[idx:]
                if ref.prefix and author_part.startswith(ref.prefix):
                    author_part = author_part[len(ref.prefix):].lstrip()
                if ref.suppress_author:
                    return f"{m.group(1)}{m.group(2)}{rest}{m.group(4)}{m.group(5)}"
                author_part = author_part.replace(" &amp; ", " and ").replace(" & ", " and ")
                pre = f"{ref.prefix} " if ref.prefix else ""
                return f"{m.group(1)}{pre}{author_part} {m.group(2)}{rest}{m.group(4)}{m.group(5)}"
            return h
        if narrative:  # numeric / label styles: "Phillips et al. [3]"
            canon = resolved.get(ref.key)
            who = authors_short(records.get(canon, {})).replace(" &amp; ", " and ") if canon else ""
            return f"{who} {h}" if who else h  # record fields are already HTML-escaped
        return h

    # convenience ------------------------------------------------------------
    def cite_strings(self, cites: list[Any], style: str | None = None, **kw) -> dict:
        groups = []
        for c in cites:
            if isinstance(c, (list, tuple)):
                groups.append(CitationGroup(refs=[CiteRef(key=str(k).lstrip("@")) for k in c], raw="; ".join(map(str, c))))
            else:
                groups.append(parse_cite_string(str(c)))
        return self.render(groups, style, **kw)

    def bibliography(self, keys: list[str], style: str | None = None, output: str = "text") -> dict:
        return self.render([], style, output=output, bibliography=True, extra_bib_keys=keys)
