"""Recognize and normalize scholarly identifiers (DOI, arXiv, ISBN, PMID, ADS bibcode, URL)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

# '<' and '>' are legal in DOIs: older GSA, AGU, Elsevier, and Wiley DOIs follow the SICI pattern,
# e.g. 10.1130/0091-7613(2001)029<0987:HSESAT>2.0.CO;2. normalize_doi() keeps a <...> segment only when it has that
# shape, so markup after a DOI in HTML ('10.1/abc</a>') is still cut off.
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"']+)", re.I)
_SICI_SEGMENT = re.compile(r"<[^<>\s/:]*:[^<>\s/]*>")  # '<0987:HSESAT>', Wiley's '<347::AID-JBM10>'; not '<https://'
_DOI_PREFIX = re.compile(
    r"^(?:https?://)?(?:dx\.|www\.)?(?:doi\.org|doi\.acm\.org|doi\.wiley\.com|hdl\.handle\.net)/|^doi:\s*|^doi\s+",
    re.I,
)
ARXIV_NEW_RE = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(v\d+)?(?![\d])", re.I)
ARXIV_OLD_RE = re.compile(r"\b([a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?\b", re.I)
ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf|html)/([^\s?#]+?)(?:\.pdf)?(?:[?#].*)?$", re.I)
ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$", re.I)
PMID_RE = re.compile(r"^(?:pmid:?\s*)?(\d{1,9})$", re.I)
PMCID_RE = re.compile(r"^(?:pmcid:?\s*)?(PMC\d+)$", re.I)
BIBCODE_RE = re.compile(r"^(\d{4}[A-Za-z&.]{1,5}[A-Za-z0-9&.]{0,9}[A-Za-z0-9.]{0,5}[A-Z.]?)$")


def normalize_doi(s: str | None) -> str | None:
    """Return a bare DOI ('10.1016/j.icarus.2014.11.027') or None.

    Handles doi.org URLs, 'doi:' prefixes, URL-encoding, and trailing punctuation
    picked up from prose, while keeping balanced parentheses that belong to the DOI
    (e.g. '10.1016/S0019-1035(03)00123-4').
    """
    if not s:
        return None
    s = unquote(str(s).strip())
    s = _DOI_PREFIX.sub("", s).strip()
    m = DOI_RE.search(s)
    if not m:
        return None
    doi = _cut_at_markup(m.group(1))
    # Trim trailing punctuation that is not part of the DOI.
    while doi and doi[-1] in ".,;:]}>'\"":
        doi = doi[:-1]
    while doi.endswith(")") and doi.count("(") < doi.count(")"):
        doi = doi[:-1]
    # Strip common URL tails publishers append after the DOI.
    doi = re.sub(r"/(?:abstract|full|pdf|epdf|html|meta)$", "", doi, flags=re.I)
    return doi or None


def _cut_at_markup(doi: str) -> str:
    """Keep SICI '<SSSS:TTTT>' segments; cut at any other '<' or '>' (HTML tags, '<doi>' wrappers)."""
    i = 0
    while True:
        j = min([k for k in (doi.find("<", i), doi.find(">", i)) if k >= 0], default=-1)
        if j < 0:
            return doi
        m = _SICI_SEGMENT.match(doi, j)
        if doi[j] == "<" and m:
            i = m.end()
            continue
        return doi[:j]


def doi_key(doi: str | None) -> str:
    """DOIs are case-insensitive; compare on the lowercased form."""
    d = normalize_doi(doi)
    return d.lower() if d else ""


def normalize_arxiv(s: str | None) -> str | None:
    """Return a bare arXiv id without version ('2208.03533', 'hep-th/9901001')."""
    if not s:
        return None
    s = str(s).strip()
    m = ARXIV_DOI_RE.match(normalize_doi(s) or "")
    if m:
        s = m.group(1)
    m = ARXIV_URL_RE.search(s)
    if m:
        s = m.group(1)
    s = re.sub(r"^arxiv:\s*", "", s, flags=re.I)
    m = ARXIV_NEW_RE.search(s)
    if m and (s.lower().startswith(m.group(1)) or "arxiv" in s.lower() or len(s) <= 16):
        return m.group(1)
    m = ARXIV_OLD_RE.match(s)
    if m:
        return m.group(1)
    return None


def _isbn10_ok(d: str) -> bool:
    if not re.fullmatch(r"\d{9}[\dX]", d):
        return False
    total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(d))
    return total % 11 == 0


def _isbn13_ok(d: str) -> bool:
    if not re.fullmatch(r"\d{13}", d):
        return False
    total = sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(d))
    return total % 10 == 0


def normalize_isbn(s: str | None) -> str | None:
    """Return a valid ISBN-13 string or None."""
    if not s:
        return None
    d = re.sub(r"[^0-9Xx]", "", re.sub(r"^isbn(?:-1[03])?:?", "", str(s).strip(), flags=re.I)).upper()
    if len(d) == 13 and _isbn13_ok(d):
        return d
    if len(d) == 10 and _isbn10_ok(d):
        core = "978" + d[:9]
        check = (10 - sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(core)) % 10) % 10
        return core + str(check)
    return None


def normalize_pmid(s: str | None) -> str | None:
    if not s:
        return None
    m = PMID_RE.match(str(s).strip())
    return m.group(1) if m else None


def looks_like_bibcode(s: str | None) -> bool:
    s = (s or "").strip()
    return len(s) == 19 and bool(re.match(r"^\d{4}[A-Za-z&]", s)) and bool(BIBCODE_RE.match(s))


@dataclass
class Identifier:
    kind: str  # doi | arxiv | isbn | pmid | pmcid | bibcode | url
    value: str


def detect(text: str) -> Identifier | None:
    """Work out what kind of identifier a user-supplied string is."""
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    # arXiv first: arXiv DOIs and URLs should resolve through the arXiv API too.
    if "arxiv" in low:
        a = normalize_arxiv(t)
        if a:
            return Identifier("arxiv", a)
    if low.startswith(("isbn", "978", "979")) or re.fullmatch(r"[\d\- ]{9,17}[\dXx]", t):
        i = normalize_isbn(t)
        if i:
            return Identifier("isbn", i)
    if low.startswith("pmcid") or low.startswith("pmc"):
        m = PMCID_RE.match(t)
        if m:
            return Identifier("pmcid", m.group(1).upper())
    if low.startswith("pmid"):
        p = normalize_pmid(t)
        if p:
            return Identifier("pmid", p)
    d = normalize_doi(t)
    if d and (low.startswith(("10.", "doi", "http")) and ("doi" in low or low.startswith("10.") or "/10." in low)):
        return Identifier("doi", d)
    if looks_like_bibcode(t):
        return Identifier("bibcode", t)
    if low.startswith(("http://", "https://", "www.")):
        url = t if low.startswith("http") else "https://" + t
        a = normalize_arxiv(url) if "arxiv.org" in low else None
        if a:
            return Identifier("arxiv", a)
        if "adsabs.harvard.edu" in low:
            m = re.search(r"/abs/([^/?#]{19})", url)
            if m:
                return Identifier("bibcode", unquote(m.group(1)))
        m = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", url)
        if m:
            return Identifier("pmid", m.group(1))
        if d and "/10." in url:
            return Identifier("doi", d)
        return Identifier("url", url)
    if d:
        return Identifier("doi", d)
    if re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", t):
        return Identifier("arxiv", t.split("v")[0])
    if re.fullmatch(r"\d{1,9}", t):
        return Identifier("pmid", t)
    return None


def is_junk_url(url: str | None) -> bool:
    """URLs that are not usable links (Web of Science placeholders, empty schemes)."""
    if not url:
        return False
    u = url.strip()
    if u.startswith("<Go to ISI>") or u.lower().startswith(("isi:", "wos:")):
        return True
    p = urlparse(u)
    return p.scheme not in ("http", "https", "ftp", "file") or not p.netloc and p.scheme != "file"
