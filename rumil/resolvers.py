"""Look up authoritative metadata from scholarly registries.

Every function returns CSL-JSON built from the registry's own record, never from
guesswork.  Sources: Crossref, DataCite, doi.org content negotiation (all other DOI
agencies), arXiv, NASA ADS (optional token), Open Library / Google Books (ISBN),
PubMed, and citation meta tags on web pages.
"""

from __future__ import annotations

import html as htmlmod
import io
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import quote

from .csl import clean_item, make_date, parse_date, parse_name, today_date, year_of
from .http import Http, HttpError
from .ids import DOI_RE, detect, normalize_arxiv, normalize_doi, normalize_isbn
from .textutil import collapse_ws, family_matches, strip_markup, title_in_text, title_similarity


class NotFoundError(Exception):
    """The identifier does not exist in any registry we can reach."""


@dataclass
class Resolution:
    csl: dict
    source: str  # crossref | datacite | doi.org | arxiv | ads | openlibrary | googlebooks | pubmed | web | github
    identifier: str = ""
    registry_record: dict = field(default_factory=dict)  # selected raw fields (retraction info, relations, ...)
    warnings: list[str] = field(default_factory=list)
    authoritative: bool = True  # False when metadata came only from web-page tags
    pdf_url: str | None = None


_ALLOWED_TAGS = re.compile(r"</?(?:i|b|sub|sup|sc)>", re.I)


def clean_title_markup(s: str | None) -> str:
    """Keep the inline markup CSL understands (<i>, <b>, <sub>, <sup>, <sc>); drop everything else."""
    if not s:
        return ""
    s = re.sub(r"<mml:math.*?</mml:math>", lambda m: strip_markup(m.group(0)), str(s), flags=re.S | re.I)
    keep = {}

    def _stash(m):
        k = f"\x00{len(keep)}\x00"
        keep[k] = m.group(0).lower()
        return k

    s = _ALLOWED_TAGS.sub(_stash, s)
    s = re.sub(r"<[^>]+>", "", s)
    s = htmlmod.unescape(s)
    for k, v in keep.items():
        s = s.replace(k, v)
    return collapse_ws(s)


def fix_caps(name: str, given: bool = False) -> str:
    """Registry records sometimes carry names in capitals ('CINTALA', 'MARK J.'); give them normal case.
    Given names of 2-3 capitals ('JE') are initials and are left for clean_item to expand to 'J. E.'."""
    if not name or not name.isupper() or len(re.sub(r"[^A-Za-z]", "", name)) < 2:
        return name
    if given and re.fullmatch(r"[A-Z]{2,3}", name):
        return name
    out = name.title()  # handles hyphens and apostrophes: RIVERA-VALENTIN -> Rivera-Valentin, O'NEIL -> O'Neil
    out = re.sub(r"\bMc([a-z])", lambda m: "Mc" + m.group(1).upper(), out)
    return re.sub(r"\b(Van|Von|De|Der|Den|Du|Da|Di|La|Le)\b(?= \w)", lambda m: m.group(1).lower(), out)


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


# ===================================================================== Crossref
CROSSREF_TYPES = {
    "journal-article": "article-journal", "proceedings-article": "paper-conference", "book-chapter": "chapter",
    "book-section": "chapter", "book-part": "chapter", "posted-content": "article", "dataset": "dataset",
    "report": "report", "report-component": "report", "book": "book", "monograph": "book", "edited-book": "book",
    "reference-book": "book", "book-set": "book", "book-series": "book", "reference-entry": "entry",
    "dissertation": "thesis", "component": "document", "standard": "standard", "peer-review": "review",
    "journal-issue": "periodical", "journal": "periodical", "proceedings": "book", "other": "document",
    "database": "dataset", "grant": "document", "report-series": "report",
}


def crossref_to_csl(m: dict) -> dict:
    ctype = CROSSREF_TYPES.get(m.get("type", ""), "document")
    title = clean_title_markup(_first(m.get("title")))
    sub = clean_title_markup(_first(m.get("subtitle")))
    if sub and sub.lower() not in title.lower():
        title = f"{title}: {sub}"
    csl: dict = {"type": ctype, "title": title}

    def names(lst):
        out = []
        for a in lst or []:
            if a.get("family"):
                n = {"family": fix_caps(a["family"])}
                if a.get("given"):
                    n["given"] = fix_caps(a["given"], given=True)
                if a.get("suffix"):
                    n["suffix"] = a["suffix"]
                out.append(n)
            elif a.get("name"):
                out.append({"literal": a["name"]})
        return out

    if m.get("author"):
        csl["author"] = names(m["author"])
    if m.get("editor"):
        csl["editor"] = names(m["editor"])
    ct = clean_title_markup(_first(m.get("container-title")))
    if ct:
        csl["container-title"] = ct
    sct = _first(m.get("short-container-title"))
    if sct and sct != ct:
        csl["container-title-short"] = sct
    for src in ("issued", "published-print", "published-online", "published", "created"):
        dp = (m.get(src) or {}).get("date-parts")
        if dp and dp[0] and dp[0][0]:
            csl["issued"] = {"date-parts": [[int(x) for x in dp[0] if x]]}
            break
    for k in ("volume", "issue", "page", "publisher", "language"):
        if m.get(k):
            csl[k] = str(m[k])
    if re.fullmatch(r"\d+-\d+", csl.get("page", "")):  # '960-0968' -> '960-968'
        csl["page"] = "-".join(p.lstrip("0") or "0" for p in csl["page"].split("-"))
    an = str(m.get("article-number") or "").strip()
    # AGU records from about 1999-2018 carry the manuscript number (2006JE002868) as the article number. It is not
    # the citation number printed on the paper (E10007), so it must never be used as the page.
    if not csl.get("page") and an and not is_agu_manuscript_number(an):
        csl["page"] = an
    if csl.get("page") and is_agu_manuscript_number(csl["page"]):
        del csl["page"]
    if m.get("publisher-location"):
        csl["publisher-place"] = m["publisher-location"]
    if m.get("DOI"):
        csl["DOI"] = m["DOI"]
        csl["URL"] = f"https://doi.org/{m['DOI']}"
    if m.get("ISSN"):
        csl["ISSN"] = m["ISSN"][0]
    if m.get("ISBN"):
        csl["ISBN"] = m["ISBN"][0]
    if m.get("abstract"):
        csl["abstract"] = strip_markup(re.sub(r"<jats:title>.*?</jats:title>", "", m["abstract"], flags=re.S))
    ev = m.get("event")
    if isinstance(ev, dict) and ev.get("name"):
        csl["event-title"] = ev["name"]
        if ev.get("location"):
            csl["event-place"] = ev["location"]
    if ctype == "article":  # preprints: name the server
        inst = _first(m.get("institution"))
        server = (inst or {}).get("name") if isinstance(inst, dict) else None
        csl["publisher"] = server or m.get("group-title") or m.get("publisher") or "Preprint"
        csl["genre"] = "Preprint"
    if ctype == "thesis":
        inst = _first(m.get("institution"))
        if isinstance(inst, dict) and inst.get("name"):
            csl["publisher"] = inst["name"]
        if m.get("degree"):
            csl["genre"] = _first(m["degree"])
    return clean_item(csl)


def crossref_record_extras(m: dict) -> dict:
    """Retractions, corrections, and preprint/published-version relations from a Crossref record."""
    updates = []
    for u in m.get("updated-by") or []:
        d = (u.get("updated") or {}).get("date-parts") or [[None]]
        updates.append({"type": u.get("type"), "label": u.get("label"), "notice_doi": u.get("DOI"),
                        "source": u.get("source"), "date": "-".join(str(x) for x in d[0] if x)})
    rel = m.get("relation") or {}

    def rel_dois(name):
        return [r.get("id") for r in rel.get(name, []) if r.get("id-type", "doi") == "doi" and r.get("id")]

    title = _first(m.get("title")) or ""
    return {
        "updates": updates,
        "notice_for": notice_targets(m),
        "retracted": any((u["type"] or "").lower() in ("retraction", "withdrawal", "removal") for u in updates)
        or bool(re.match(r"^\s*(retracted|withdrawn)\b", title, re.I)),
        "published_version_dois": rel_dois("is-preprint-of"),
        "preprint_dois": rel_dois("has-preprint"),
        "crossref_type": m.get("type"),
        "is_referenced_by_count": m.get("is-referenced-by-count"),
    }


_NOTICE_TYPES = {"correction", "erratum", "corrigendum", "addendum", "retraction", "withdrawal", "removal",
                 "expression_of_concern", "expression-of-concern", "clarification", "partial_retraction"}
_NOTICE_TITLE = re.compile(r"^\s*(?:author\s+|publisher\s+)?(?:erratum|errata|corrigendum|corrigenda|correction|addendum|"
                           r"retraction(?:\s+note)?|expression\s+of\s+concern)\b\s*(?:to|for|of|on)?\s*[:\-\u2013\u2014]?", re.I)


def notice_targets(m: dict) -> list[str]:
    """DOIs that this Crossref record corrects or retracts (an erratum points at its article with 'update-to')."""
    return [u["DOI"] for u in m.get("update-to") or []
            if u.get("DOI") and (u.get("type") or "").lower() in _NOTICE_TYPES and
            (u["DOI"] or "").lower() != (m.get("DOI") or "").lower()]


def is_notice_title(title: str | None) -> bool:
    """'Erratum: ...', 'Corrigendum: ...', 'Author Correction: ...', 'Retraction Note: ...'."""
    return bool(_NOTICE_TITLE.match(strip_markup(title or "")))


# Fields Crossref allows in `select` on /works searches. Asking for any other field (e.g. 'institution') makes
# Crossref reject the whole request with HTTP 400.
CROSSREF_SEARCH_FIELDS = ("DOI", "title", "subtitle", "author", "editor", "issued", "published-print",
                          "published-online", "container-title", "short-container-title", "volume", "issue", "page",
                          "article-number", "type", "score", "publisher", "ISSN", "ISBN", "event", "update-to")
CROSSREF_SELECTABLE = frozenset(
    "abstract URL resource member posted score created degree update-policy short-title license ISSN container-title "
    "issued update-to issue prefix approved indexed article-number clinical-trial-number accepted author group-title DOI "
    "is-referenced-by-count updated-by event chair standards-body original-title funder translator published archive "
    "published-print alternative-id subject subtitle published-online publisher-location content-domain reference title "
    "link type publisher volume references-count ISBN issn-type assertion deposited page contributor content-created "
    "short-container-title relation editor".split())  # as listed by Crossref's own validation error, October 2026

_AGU_MS_NUMBER = re.compile(r"(?:19|20)\d{2}(?:JA|JB|JC|JD|JE|JF|JG|GL|GB|PA|RG|RS|WR|TC|EF|EA|SW|GC|GH|AV|MS|CN)\d{6}")


def is_agu_manuscript_number(v) -> bool:
    """'2006JE002868' (AGU's manuscript number, not a citation number). 'e2019JE006307' is a real AGU e-number."""
    return bool(_AGU_MS_NUMBER.fullmatch(str(v or "").strip()))


class Resolver:
    def __init__(self, http: Http | None = None, ads_token: str = ""):
        self.http = http or Http()
        self.ads_token = (ads_token or "").strip()

    # --------------------------------------------------------------- DOIs
    def crossref_work(self, doi: str) -> Resolution | None:
        data = self.http.get_json(f"https://api.crossref.org/works/{quote(doi, safe='/:()')}",
                                  params={"mailto": self.http.contact_email or None})
        if not data or data.get("status") != "ok":
            return None
        m = data["message"]
        return Resolution(crossref_to_csl(m), "crossref", doi, crossref_record_extras(m))

    def datacite(self, doi: str) -> Resolution | None:
        data = self.http.get_json(f"https://api.datacite.org/dois/{quote(doi, safe='/:()')}")
        if not data or "data" not in data:
            return None
        a = data["data"].get("attributes", {})
        types = a.get("types") or {}
        ctype = types.get("citeproc") or "dataset"
        if ctype == "article":
            ctype = "article"
        titles = a.get("titles") or [{}]
        csl: dict = {"type": ctype, "title": clean_title_markup(titles[0].get("title"))}
        authors = []
        for c in a.get("creators") or []:
            if c.get("familyName"):
                authors.append({"family": c["familyName"], "given": c.get("givenName", "")})
            elif c.get("name"):
                if c.get("nameType") == "Organizational" or "," not in c["name"]:
                    authors.append({"literal": c["name"]})
                else:
                    authors.append(parse_name(c["name"]))
        if authors:
            csl["author"] = authors
        pub = a.get("publisher")
        if isinstance(pub, dict):
            pub = pub.get("name")
        if pub:
            csl["publisher"] = pub
        issued = None
        for d in a.get("dates") or []:
            if d.get("dateType") == "Issued" and d.get("date"):
                issued = parse_date(d["date"])
        if not issued and a.get("publicationYear"):
            issued = make_date(int(a["publicationYear"]))
        if issued:
            csl["issued"] = issued
        if a.get("version"):
            csl["version"] = str(a["version"])
        cont = a.get("container") or {}
        if cont.get("title"):
            csl["container-title"] = cont["title"]
        for d in a.get("descriptions") or []:
            if d.get("descriptionType") == "Abstract" and d.get("description"):
                csl["abstract"] = strip_markup(d["description"])
                break
        csl["DOI"] = a.get("doi") or doi
        csl["URL"] = f"https://doi.org/{csl['DOI']}"
        rel = [r.get("relatedIdentifier") for r in (a.get("relatedIdentifiers") or [])
               if r.get("relationType") == "IsVersionOf" and r.get("relatedIdentifierType") == "DOI"]
        extras = {"updates": [], "retracted": False, "concept_doi": rel[0] if rel else None,
                  "published_version_dois": [], "preprint_dois": []}
        return Resolution(clean_item(csl), "datacite", doi, extras)

    def doiorg_csl(self, doi: str) -> Resolution | None:
        resp = self.http.request(f"https://doi.org/{quote(doi, safe='/:()')}",
                                 accept="application/vnd.citationstyles.csl+json")
        if resp.status in (404, 406) or not resp.ok:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        data.pop("id", None)
        for k in ("title", "container-title"):
            if isinstance(data.get(k), list):
                data[k] = _first(data[k]) or ""
        data["DOI"] = data.get("DOI") or doi
        return Resolution(clean_item(data), "doi.org", doi, {"updates": [], "retracted": False,
                                                              "published_version_dois": [], "preprint_dois": []})

    def doi_exists(self, doi: str) -> bool | None:
        """Ask the DOI handle system directly whether a DOI is registered (None = could not tell)."""
        try:
            resp = self.http.request(f"https://doi.org/api/handles/{quote(doi, safe='/:()')}")
        except HttpError:
            return None
        try:
            code = resp.json().get("responseCode")
        except ValueError:
            return None
        if code == 1:
            return True
        if code in (100, 200):
            return False
        return None

    def resolve_doi(self, doi: str) -> Resolution:
        doi = normalize_doi(doi) or doi
        errors = []
        for fn in (self.crossref_work, self.datacite, self.doiorg_csl):
            try:
                r = fn(doi)
            except HttpError as e:
                errors.append(str(e))
                continue
            if r and (r.csl.get("title") or r.csl.get("author")):
                return r
        exists = self.doi_exists(doi)
        if exists is False:
            raise NotFoundError(f"DOI {doi} is not registered (doi.org returned 'handle not found')")
        if errors and exists is None:
            raise HttpError("; ".join(errors))
        raise NotFoundError(f"DOI {doi} exists but no registry returned metadata for it")

    # -------------------------------------------------------------- search
    def crossref_search(self, *, query: str = "", title: str = "", author: str = "", year: int | None = None,
                        container: str = "", rows: int = 5) -> list[tuple[dict, float]]:
        params = {"rows": rows, "mailto": self.http.contact_email or None, "select": ",".join(CROSSREF_SEARCH_FIELDS)}
        bib = " ".join(x for x in [title, query, container, str(year or "")] if x).strip()
        if bib:
            params["query.bibliographic"] = bib
        if author:
            params["query.author"] = author
        if year:
            params["filter"] = f"from-pub-date:{int(year) - 1},until-pub-date:{int(year) + 1}"
        data = self.http.get_json("https://api.crossref.org/works", params=params)
        out = []
        for m in ((data or {}).get("message") or {}).get("items", []):
            csl = crossref_to_csl(m)
            targets = notice_targets(m)
            if targets:
                csl["_notice_for"] = targets  # private marker; removed before anything is stored
            out.append((csl, float(m.get("score") or 0)))
        return out

    def _handle_notices(self, cands: list[dict], want: str) -> None:
        """Mark correction/retraction notices among search hits, and make sure the article they correct is offered.

        A notice is recognized by Crossref's 'update-to' link, by a title such as 'Erratum: ...', or by being a one-page
        record whose title matches a longer record from the same year (Nature errata often reuse the article's
        title: 10.1038/nature04075, page 882, corrects 10.1038/nature03639, pages 504-509)."""
        real = [c for c in cands if "csl" in c]
        for c in real:
            csl = c["csl"]
            targets = csl.pop("_notice_for", None)
            if targets or is_notice_title(csl.get("title")):
                c["notice_for"] = targets or []
        for c in real:
            if "notice_for" in c or _page_span(c["csl"].get("page")) > 1:
                continue
            for o in real:
                if o is c or "notice_for" in o or _page_span(o["csl"].get("page")) <= 2:
                    continue
                if title_similarity(c["csl"].get("title"), o["csl"].get("title")) >= 0.97 and \
                        abs((year_of(c["csl"]) or 0) - (year_of(o["csl"]) or 0)) <= 1 and \
                        (c["csl"].get("container-title") or "").lower() == (o["csl"].get("container-title") or "").lower():
                    c["notice_for"] = [o["csl"].get("DOI")] if o["csl"].get("DOI") else []
                    break
        have = {(c["csl"].get("DOI") or "").lower() for c in real}
        for c in real:
            for doi in c.get("notice_for") or []:
                if doi and doi.lower() not in have:
                    have.add(doi.lower())
                    try:
                        r = self.crossref_work(doi)
                    except HttpError:
                        r = None
                    if r:
                        cands.append({"csl": r.csl, "source": "crossref", "registry_score": c.get("registry_score"),
                                      "corrected_by": c["csl"].get("DOI")})

    # --------------------------------------------------------------- arXiv
    def arxiv(self, arxiv_id: str) -> Resolution | None:
        aid = normalize_arxiv(arxiv_id) or arxiv_id
        resp = self.http.request("https://export.arxiv.org/api/query", params={"id_list": aid, "max_results": 1})
        if not resp.ok:
            raise HttpError(f"arXiv returned HTTP {resp.status}", resp.status)
        entries = parse_arxiv_atom(resp.text)
        if not entries:
            return None
        csl, extras = entries[0]
        return Resolution(csl, "arxiv", aid, extras)

    def arxiv_search(self, title: str, author: str = "", rows: int = 3) -> list[tuple[dict, float]]:
        clean_title = re.sub(r"[^A-Za-z0-9 ]+", " ", title or "")
        clean_author = re.sub(r"[^A-Za-z]+", "_", _author_family(author)) if author else ""
        terms = [f'ti:"{clean_title}"'] if title else []
        if clean_author:
            terms.append(f"au:{clean_author}")
        if not terms:
            return []
        resp = self.http.request("https://export.arxiv.org/api/query",
                                 params={"search_query": " AND ".join(terms), "max_results": rows})
        if not resp.ok:
            return []
        return [(csl, title_similarity(title, csl.get("title"))) for csl, _ in parse_arxiv_atom(resp.text)]

    # ----------------------------------------------------------------- ADS
    def ads_query(self, q: str, rows: int = 5) -> list[dict]:
        if not self.ads_token:
            return []
        data = self.http.get_json(
            "https://api.adsabs.harvard.edu/v1/search/query",
            params={"q": q, "rows": rows, "fl": "bibcode,title,author,year,pub,volume,issue,page,doi,doctype,pubdate,identifier,abstract,editor,publisher,isbn"},
            headers={"Authorization": f"Bearer {self.ads_token}"})
        return ((data or {}).get("response") or {}).get("docs", [])

    def ads_by_doi(self, doi: str) -> dict | None:
        """The NASA ADS record for a DOI (raw fields), or None. Cached, since verification may ask twice."""
        if not self.ads_token or not doi:
            return None
        key = doi.lower()
        cache = self.__dict__.setdefault("_ads_doi_cache", {})
        if key not in cache:
            docs = self.ads_query(f'doi:"{doi}"', rows=1)
            cache[key] = docs[0] if docs else None
        return cache[key]

    def ads_bibcode(self, bibcode: str) -> Resolution | None:
        docs = self.ads_query(f'bibcode:"{bibcode}"', rows=1)
        if not docs:
            return None
        csl = ads_to_csl(docs[0])
        return Resolution(csl, "ads", bibcode, {"bibcode": bibcode, "updates": [], "retracted": False,
                                                 "published_version_dois": [], "preprint_dois": []})

    def ads_search(self, title: str, author: str = "", year: int | None = None, rows: int = 5) -> list[tuple[dict, float]]:
        if not self.ads_token or not title:
            return []
        clean = title.replace('"', "")[:200]
        q = f'title:"{clean}"'
        if author:
            q += f' author:"^{author}"'
        if year:
            q += f" year:{int(year) - 1}-{int(year) + 1}"
        return [(ads_to_csl(d), title_similarity(title, _first(d.get("title")))) for d in self.ads_query(q, rows)]

    # ---------------------------------------------------------------- ISBN
    def isbn(self, isbn: str) -> Resolution | None:
        isbn = normalize_isbn(isbn) or isbn
        try:
            data = self.http.get_json("https://openlibrary.org/api/books",
                                      params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"})
        except HttpError:
            data = None
        rec = (data or {}).get(f"ISBN:{isbn}")
        if rec:
            csl = {"type": "book", "title": rec.get("title", ""), "ISBN": isbn}
            if rec.get("subtitle"):
                csl["title"] += f": {rec['subtitle']}"
            authors = [parse_name(a["name"]) for a in rec.get("authors", []) if a.get("name")]
            if authors:
                csl["author"] = authors
            if rec.get("publishers"):
                csl["publisher"] = rec["publishers"][0].get("name")
            if rec.get("publish_places"):
                csl["publisher-place"] = rec["publish_places"][0].get("name")
            if rec.get("publish_date"):
                csl["issued"] = parse_date(rec["publish_date"])
            if rec.get("number_of_pages"):
                csl["number-of-pages"] = str(rec["number_of_pages"])
            r = Resolution(clean_item(csl), "openlibrary", isbn)
            # Open Library often lists only the first author; Google Books is usually complete.
            gb = self._googlebooks(isbn)
            if gb and len(gb.csl.get("author", [])) > len(r.csl.get("author", [])):
                r.csl["author"] = gb.csl["author"]
            return r
        return self._googlebooks(isbn)

    def _googlebooks(self, isbn: str) -> Resolution | None:
        try:
            data = self.http.get_json("https://www.googleapis.com/books/v1/volumes", params={"q": f"isbn:{isbn}"})
        except HttpError:
            return None
        items = (data or {}).get("items") or []
        if not items:
            return None
        v = items[0].get("volumeInfo", {})
        csl = {"type": "book", "title": v.get("title", ""), "ISBN": isbn}
        if v.get("subtitle"):
            csl["title"] += f": {v['subtitle']}"
        if v.get("authors"):
            csl["author"] = [parse_name(a) for a in v["authors"]]
        if v.get("publisher"):
            csl["publisher"] = v["publisher"]
        if v.get("publishedDate"):
            csl["issued"] = parse_date(v["publishedDate"])
        if v.get("pageCount"):
            csl["number-of-pages"] = str(v["pageCount"])
        return Resolution(clean_item(csl), "googlebooks", isbn)

    # -------------------------------------------------------------- PubMed
    def pubmed(self, pmid: str) -> Resolution | None:
        data = self.http.get_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                                  params={"db": "pubmed", "id": pmid, "retmode": "json"})
        rec = ((data or {}).get("result") or {}).get(str(pmid))
        if not rec or rec.get("error"):
            return None
        doi = next((a["value"] for a in rec.get("articleids", []) if a.get("idtype") == "doi"), None)
        if doi:
            try:
                r = self.resolve_doi(doi)
                r.csl["PMID"] = str(pmid)
                return r
            except (NotFoundError, HttpError):
                pass
        csl = {"type": "article-journal", "title": rec.get("title", "").rstrip("."),
               "container-title": rec.get("fulljournalname") or rec.get("source"), "volume": rec.get("volume"),
               "issue": rec.get("issue"), "page": rec.get("pages"), "issued": parse_date(rec.get("pubdate")),
               "PMID": str(pmid)}
        authors = []
        for a in rec.get("authors", []):
            parts = a.get("name", "").rsplit(" ", 1)
            if len(parts) == 2 and parts[1].isupper():
                authors.append({"family": parts[0], "given": " ".join(c + "." for c in parts[1])})
            elif a.get("name"):
                authors.append({"literal": a["name"]})
        if authors:
            csl["author"] = authors
        return Resolution(clean_item(csl), "pubmed", pmid)

    # ----------------------------------------------------------------- web
    def webpage(self, url: str) -> Resolution:
        """Resolve a URL: DOI or arXiv id in the URL, citation meta tags, a PDF's DOI, or plain page metadata."""
        ident = detect(url)
        if ident and ident.kind == "arxiv":
            r = self.arxiv(ident.value)
            if r:
                return r
        if ident and ident.kind == "doi":
            try:
                return self.resolve_doi(ident.value)
            except NotFoundError:
                pass
        gh = re.match(r"https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+)", url)
        if gh:
            r = self._github(gh.group(1), gh.group(2).removesuffix(".git"), url)
            if r:
                return r
        resp = self.http.request(url, accept="text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8")
        if not resp.ok:
            raise NotFoundError(f"{url} returned HTTP {resp.status}")
        ctype = resp.headers.get("content-type", "")
        if "pdf" in ctype or resp.body[:5] == b"%PDF-":
            return self._from_pdf_bytes(resp.body, url)
        meta = parse_html_meta(resp.text, resp.url)
        doi = meta.doi()
        if doi:
            try:
                r = self.resolve_doi(doi)
                page_title = meta.title()
                if page_title and title_similarity(page_title, r.csl.get("title")) < 0.6:
                    r.warnings.append(f"Page title '{page_title[:80]}' differs from the DOI record title; check you saved the right work")
                r.pdf_url = meta.first("citation_pdf_url")
                return r
            except NotFoundError:
                pass
        aid = meta.first("citation_arxiv_id")
        if aid:
            r = self.arxiv(aid)
            if r:
                return r
        csl = meta.to_csl(resp.url)
        res = Resolution(csl, "web", resp.url, authoritative=False, pdf_url=meta.first("citation_pdf_url"))
        res.warnings.append("No DOI or registry record found; metadata comes from the page's own tags and could not be verified")
        return res

    def _from_pdf_bytes(self, data: bytes, url: str) -> Resolution:
        """Identify a PDF: its arXiv stamp, else the first DOI on pages 1-2 whose registered title is printed in
        the PDF. (The first DOI alone is often one the paper cites, not its own.)"""
        text = pdf_text(data, max_pages=2)
        if not text:
            raise NotFoundError("Could not read text from this PDF; add it by DOI or title instead")
        m = re.search(r"arXiv:\s*(\d{4}\.\d{4,5})(v\d+)?\s*\[", text) or re.search(r"arXiv:\s*(\d{4}\.\d{4,5})", text)
        if m:
            r = self.arxiv(m.group(1))
            if r and title_in_text(r.csl.get("title"), text):
                r.pdf_url = url
                return r
        seen = []
        for dm in DOI_RE.finditer(text):
            d = normalize_doi(dm.group(1))
            if d and d.lower() not in seen:
                seen.append(d.lower())
        for d in seen[:8]:
            try:
                r = self.resolve_doi(d)
            except (NotFoundError, HttpError):
                continue
            if title_in_text(r.csl.get("title"), text):
                r.pdf_url = url
                return r
        raise NotFoundError("Could not tell which work this PDF is (no DOI or arXiv id whose title appears in it); "
                            "add it by DOI or title instead")

    def lpsc_abstract(self, year: int, number: int) -> tuple[str, str] | None:
        """Fetch an LPSC abstract PDF from the Lunar and Planetary Institute. Returns (url, text) or None."""
        for host in ("www.hou.usra.edu", "www.lpi.usra.edu"):
            for num in (f"{number:04d}", str(number)):
                url = f"https://{host}/meetings/lpsc{year}/pdf/{num}.pdf"
                try:
                    resp = self.http.request(url, retries=1, timeout=30)
                except HttpError:
                    continue
                if resp.ok and resp.body[:5] == b"%PDF-":
                    return url, pdf_text(resp.body, max_pages=2)
                if num == str(number):
                    break
        return None

    def _github(self, owner: str, repo: str, url: str) -> Resolution | None:
        try:
            data = self.http.get_json(f"https://api.github.com/repos/{owner}/{repo}")
        except HttpError:
            return None
        if not data or "full_name" not in data:
            return None
        csl = {"type": "software", "title": data["full_name"], "author": [{"literal": data["owner"]["login"]}],
               "publisher": "GitHub", "URL": data.get("html_url") or url, "accessed": today_date()}
        if data.get("description"):
            csl["abstract"] = data["description"]
        when = data.get("pushed_at") or data.get("created_at")
        if when:
            csl["issued"] = parse_date(when[:10])
        r = Resolution(clean_item(csl), "github", url, authoritative=False)
        r.warnings.append("Software on GitHub: if the project has a Zenodo DOI, cite that instead")
        return r

    # ----------------------------------------------------------- dispatch
    def resolve(self, identifier: str) -> Resolution:
        ident = detect(identifier)
        if not ident:
            raise NotFoundError(f"'{identifier}' is not a DOI, arXiv id, ISBN, PMID, ADS bibcode, or URL")
        if ident.kind == "doi":
            return self.resolve_doi(ident.value)
        if ident.kind == "arxiv":
            r = self.arxiv(ident.value)
            if not r:
                raise NotFoundError(f"arXiv has no record {ident.value}")
            return r
        if ident.kind == "isbn":
            r = self.isbn(ident.value)
            if not r:
                raise NotFoundError(f"No book found for ISBN {ident.value}")
            return r
        if ident.kind == "pmid":
            r = self.pubmed(ident.value)
            if not r:
                raise NotFoundError(f"PubMed has no record {ident.value}")
            return r
        if ident.kind == "bibcode":
            if not self.ads_token:
                raise NotFoundError("ADS bibcodes need an ADS API token in ~/Rumil/config.json (ads_token)")
            r = self.ads_bibcode(ident.value)
            if not r:
                raise NotFoundError(f"ADS has no record {ident.value}")
            if r.csl.get("DOI"):
                try:
                    better = self.resolve_doi(r.csl["DOI"])
                    better.registry_record["bibcode"] = ident.value
                    return better
                except (NotFoundError, HttpError):
                    pass
            return r
        if ident.kind == "url":
            return self.webpage(ident.value)
        raise NotFoundError(f"Unsupported identifier type {ident.kind}")

    def search(self, *, query: str = "", title: str = "", author: str = "", year: int | None = None,
               rows: int = 5) -> list[dict]:
        """Search registries and return candidate records with a match score against what was asked."""
        cands: list[dict] = []
        seen = set()
        try:
            for csl, score in self.crossref_search(query=query, title=title, author=author, year=year, rows=rows):
                key = (csl.get("DOI") or "").lower()
                if key in seen:
                    continue
                seen.add(key)
                cands.append({"csl": csl, "source": "crossref", "registry_score": score})
        except HttpError as e:
            cands.append({"error": f"Crossref search failed: {e}"})
        if self.ads_token and (title or query):
            try:
                for csl, _ in self.ads_search(title or query, author, year, rows=3):
                    key = (csl.get("DOI") or "").lower()
                    if key and key in seen:
                        continue
                    seen.add(key)
                    cands.append({"csl": csl, "source": "ads"})
            except HttpError:
                pass
        want = title or query
        self._handle_notices(cands, want)
        for c in cands:
            if "csl" not in c:
                continue
            csl = c["csl"]
            sim = title_similarity(want, csl.get("title")) if want else 0.0
            auth_ok = None
            if author:
                fams = [n.get("family") or n.get("literal", "") for n in csl.get("author", [])]
                auth_ok = any(family_matches(_author_family(author), f) for f in fams)
            yr = year_of(csl)
            c["match"] = {"title_similarity": round(sim, 3), "author_match": auth_ok,
                          "year_diff": (abs(yr - int(year)) if yr and year else None)}
            c["confidence"] = _confidence(sim if want else None, auth_ok, c["match"]["year_diff"])
            if c.get("notice_for") is not None:
                c["confidence"] = "low"  # an erratum or retraction notice is never the work being cited
        cands.sort(key=lambda c: -{"high": 3, "medium": 2, "low": 1}.get(c.get("confidence", "low"), 0))
        return cands


def _page_span(page) -> int:
    """Number of pages in '504-509' (6); 1 for a single page or an article number."""
    m = re.fullmatch(r"\s*(\d+)\s*[-\u2013]+\s*(\d+)\s*", str(page or ""))
    if not m:
        return 1
    a, b = int(m.group(1)), int(m.group(2))
    if b < a and len(m.group(2)) < len(m.group(1)):  # '228-36'
        b = int(m.group(1)[:len(m.group(1)) - len(m.group(2))] + m.group(2))
    return max(1, b - a + 1)


def _author_family(author: str) -> str:
    """'Phillips, M.' -> 'Phillips'; 'Michael Phillips' -> 'Phillips'; 'Phillips' -> 'Phillips'."""
    a = (author or "").strip()
    if "," in a:
        return a.split(",")[0].strip()
    return a.split()[-1] if a.split() else ""


def _confidence(sim: float | None, auth_ok: bool | None, year_diff: int | None) -> str:
    """'high' needs a near-identical title AND a matching first author AND the same year (±1).

    Without an author or a year to compare, a match is at most 'medium': short or generic titles
    ('Introduction to the special issue') match many unrelated works."""
    if sim is None:
        return "low"
    if sim >= 0.93 and auth_ok is True and year_diff is not None and year_diff <= 1:
        return "high"
    if sim >= 0.8 and auth_ok is not False:
        return "medium"
    return "low"


# =================================================================== LPSC abstracts
_LPSC_NAME = re.compile(r"lunar\s+and\s+planetary\s+science|\blpsc\b", re.I)
_LPSC_ORDINAL = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s+lunar\s+and\s+planetary\s+science", re.I)
_LPI_NAME = re.compile(r"^\s*(lpi|lunar\s+and\s+planetary\s+institute)\b", re.I)


def lpsc_info(csl: dict) -> tuple[int, int] | None:
    """(year, abstract number) for an LPSC abstract, from its meeting name, date, and page/number fields."""
    venue = " ".join(str(csl.get(k) or "") for k in ("container-title", "event-title", "event", "collection-title", "publisher"))
    if not (_LPSC_NAME.search(venue) or _LPI_NAME.search(venue)):
        return None
    m = _LPSC_ORDINAL.search(venue)
    year = 1969 + int(m.group(1)) if m else year_of(csl)
    if not year or year < 1990:
        return None
    for k in ("page", "number", "collection-number", "volume", "note"):
        v = str(csl.get(k) or "")
        mm = re.search(r"(?:#|abstract\s*(?:no\.?|number|#)?\s*)?\b(\d{3,4})\b", v, re.I)
        if mm and not (k == "volume" and len(mm.group(1)) == 4 and mm.group(1).startswith(("19", "20"))):
            return year, int(mm.group(1))
    return None


# ======================================================================= ADS map
ADS_TYPES = {"article": "article-journal", "inproceedings": "paper-conference", "abstract": "paper-conference",
             "eprint": "article", "book": "book", "inbook": "chapter", "phdthesis": "thesis", "mastersthesis": "thesis",
             "techreport": "report", "software": "software", "dataset": "dataset", "misc": "document",
             "proceedings": "book", "catalog": "dataset", "circular": "report", "newsletter": "article-magazine"}


def ads_to_csl(d: dict) -> dict:
    csl = {"type": ADS_TYPES.get(d.get("doctype", ""), "document"), "title": strip_markup(_first(d.get("title")) or "")}
    authors = [parse_name(a) for a in d.get("author") or []]
    if authors:
        csl["author"] = authors
    if d.get("pub"):
        csl["container-title"] = d["pub"]
    if d.get("pubdate"):
        pd = parse_date(d["pubdate"].replace("-00", ""))
        if pd:
            csl["issued"] = pd
    elif d.get("year"):
        csl["issued"] = make_date(int(d["year"]))
    for k, ck in (("volume", "volume"), ("issue", "issue")):
        if d.get(k):
            csl[ck] = str(d[k])
    if d.get("page"):
        csl["page"] = str(_first(d["page"]))
    if d.get("doi"):
        csl["DOI"] = _first(d["doi"])
    if d.get("bibcode"):
        csl["URL"] = f"https://ui.adsabs.harvard.edu/abs/{d['bibcode']}"
        csl["note"] = f"ADS bibcode: {d['bibcode']}"
    if d.get("abstract"):
        csl["abstract"] = strip_markup(d["abstract"])
    return clean_item(csl)


# ===================================================================== arXiv Atom
_A = "{http://www.w3.org/2005/Atom}"
_AX = "{http://arxiv.org/schemas/atom}"


def parse_arxiv_atom(xml_text: str) -> list[tuple[dict, dict]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for e in root.findall(f"{_A}entry"):
        id_url = (e.findtext(f"{_A}id") or "").strip()
        title = collapse_ws(e.findtext(f"{_A}title") or "")
        if not id_url or "arxiv.org/api/errors" in id_url or title.lower() == "error":
            continue
        m = re.search(r"arxiv\.org/abs/(.+?)(v\d+)?$", id_url)
        aid = m.group(1) if m else id_url
        version = m.group(2) if m and m.group(2) else ""
        authors = [parse_name(a.findtext(f"{_A}name") or "") for a in e.findall(f"{_A}author")]
        published = (e.findtext(f"{_A}published") or "")[:10]
        prim = e.find(f"{_AX}primary_category")
        csl = {
            "type": "article", "title": title, "author": [a for a in authors if a],
            "issued": parse_date(published), "publisher": "arXiv", "genre": "Preprint",
            "number": f"arXiv:{aid}", "DOI": f"10.48550/arXiv.{aid}", "URL": f"https://arxiv.org/abs/{aid}",
            "abstract": collapse_ws(e.findtext(f"{_A}summary") or ""),
        }
        if version:
            csl["version"] = version.lstrip("v")
        extras = {
            "arxiv_id": aid,
            "primary_category": prim.get("term") if prim is not None else None,
            "journal_ref": collapse_ws(e.findtext(f"{_AX}journal_ref") or "") or None,
            "published_version_dois": [d for d in [normalize_doi(e.findtext(f"{_AX}doi") or "")] if d],
            "updates": [], "retracted": "withdrawn" in (e.findtext(f"{_AX}comment") or "").lower(),
            "preprint_dois": [],
        }
        out.append((clean_item(csl), extras))
    return out


# ===================================================================== HTML meta
class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.title_text = ""
        self._in_title = False
        self._in_ld = False
        self._ld_buf: list[str] = []
        self.ld: list = []
        self.canonical = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            name = (a.get("name") or a.get("property") or a.get("itemprop") or "").strip().lower()
            if name and a.get("content", "").strip():
                self.meta.setdefault(name, []).append(a["content"].strip())
        elif tag == "title":
            self._in_title = True
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_ld = True
            self._ld_buf = []
        elif tag == "link" and a.get("rel", "").lower() == "canonical" and a.get("href"):
            self.canonical = a["href"]

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_ld:
            self._in_ld = False
            try:
                data = json.loads("".join(self._ld_buf))
                self.ld.extend(data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else [])
            except (ValueError, AttributeError):
                pass

    def handle_data(self, data):
        if self._in_title:
            self.title_text += data
        elif self._in_ld:
            self._ld_buf.append(data)


class PageMeta:
    def __init__(self, meta: dict[str, list[str]], title_text: str, ld: list, url: str):
        self.m, self.title_text, self.ld, self.url = meta, collapse_ws(title_text), ld, url

    def first(self, *names) -> str | None:
        for n in names:
            v = self.m.get(n.lower())
            if v:
                return v[0]
        return None

    def all(self, *names) -> list[str]:
        for n in names:
            v = self.m.get(n.lower())
            if v:
                return v
        return []

    def _ld_article(self) -> dict:
        for obj in self.ld:
            if isinstance(obj, dict):
                t = obj.get("@type")
                t = t if isinstance(t, list) else [t]
                if any(x in ("ScholarlyArticle", "Article", "NewsArticle", "Dataset", "SoftwareSourceCode", "Book",
                             "Report", "Thesis", "BlogPosting", "TechArticle", "WebPage") for x in t):
                    return obj
        return {}

    def doi(self) -> str | None:
        for n in ("citation_doi", "dc.identifier", "dc.identifier.doi", "prism.doi", "bepress_citation_doi", "eprints.id_number"):
            for v in self.all(n):
                d = normalize_doi(v)
                if d:
                    return d
        ld = self._ld_article()
        ident = ld.get("identifier") or ld.get("sameAs")
        for v in ident if isinstance(ident, list) else [ident]:
            if isinstance(v, dict):
                v = v.get("value") or v.get("@id")
            d = normalize_doi(str(v)) if v else None
            if d:
                return d
        return None

    def title(self) -> str:
        ld = self._ld_article()
        return (self.first("citation_title", "dc.title", "og:title", "twitter:title")
                or ld.get("headline") or ld.get("name") or self.title_text or "")

    def to_csl(self, url: str) -> dict:
        ld = self._ld_article()
        journal = self.first("citation_journal_title", "prism.publicationname")
        conf = self.first("citation_conference_title")
        if journal:
            ctype = "article-journal"
        elif conf:
            ctype = "paper-conference"
        elif self.first("citation_dissertation_institution"):
            ctype = "thesis"
        elif self.first("citation_technical_report_institution"):
            ctype = "report"
        elif self.first("og:type") == "article" or (ld.get("@type") in ("BlogPosting",)):
            ctype = "post-weblog" if ld.get("@type") == "BlogPosting" else "webpage"
        else:
            ctype = "webpage"
        csl: dict = {"type": ctype, "title": strip_markup(self.title())}
        names = self.all("citation_author", "dc.creator", "author", "article:author")
        if not names and ld.get("author"):
            la = ld["author"] if isinstance(ld["author"], list) else [ld["author"]]
            names = [x.get("name") if isinstance(x, dict) else str(x) for x in la]
        authors = [parse_name(n) for n in names if n and not n.startswith("http")]
        if authors:
            csl["author"] = authors
        date = self.first("citation_publication_date", "citation_date", "citation_online_date", "dc.date",
                          "article:published_time", "prism.publicationdate") or ld.get("datePublished")
        if date:
            csl["issued"] = parse_date(str(date)[:10] if re.match(r"\d{4}-\d\d-\d\d", str(date)) else date)
        if journal:
            csl["container-title"] = journal
        elif conf:
            csl["container-title"] = conf
        else:
            site = self.first("og:site_name", "citation_publisher", "dc.publisher")
            if site:
                csl["container-title"] = site
        for k, n in (("volume", "citation_volume"), ("issue", "citation_issue")):
            v = self.first(n)
            if v:
                csl[k] = v
        fp, lp = self.first("citation_firstpage"), self.first("citation_lastpage")
        if fp:
            csl["page"] = f"{fp}-{lp}" if lp and lp != fp else fp
        pub = self.first("citation_publisher", "dc.publisher", "citation_dissertation_institution",
                         "citation_technical_report_institution")
        if pub and ctype != "webpage":
            csl["publisher"] = pub
        num = self.first("citation_technical_report_number")
        if num:
            csl["number"] = num
        isbn = self.first("citation_isbn")
        if isbn:
            csl["ISBN"] = isbn
        abstract = self.first("citation_abstract", "dc.description", "description", "og:description")
        if abstract:
            csl["abstract"] = strip_markup(abstract)
        csl["URL"] = url
        csl["accessed"] = today_date()
        return clean_item(csl)


def parse_html_meta(text: str, url: str = "") -> PageMeta:
    p = _MetaParser()
    try:
        p.feed(text[:3_000_000])
    except Exception:  # malformed HTML: keep whatever was parsed
        pass
    return PageMeta(p.meta, p.title_text, p.ld, url)


def pdf_text(data: bytes, max_pages: int = 2) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((reader.pages[i].extract_text() or "") for i in range(min(max_pages, len(reader.pages))))
    except Exception:
        return ""
