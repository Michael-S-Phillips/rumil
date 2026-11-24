"""BibTeX / BibLaTeX export from CSL-JSON.

Title capitalization: most .bst files (AGU, AAS, Elsevier) lowercase titles, so
proper nouns and acronyms must be wrapped in braces to survive. Rumil protects
acronyms (CRISM, HiRISE), formulas (SiO2), capitalized words inside
sentence-case titles, and a built-in list of planetary names, missions, and
places, plus any extra words in config "protected_words".
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

from .csl import date_parts, is_etal_name
from .ids import normalize_arxiv
from .textutil import fold

PROTECT_DEFAULT = set("""
Mars Martian Earth Earth's Moon Mercury Mercury's Venus Venusian Jupiter Jovian Saturn Saturnian Uranus Neptune Pluto
Ceres Vesta Titan Europa Ganymede Callisto Io Enceladus Triton Charon Phobos Deimos Bennu Ryugu Psyche Arrokoth Sun
Hellas Argyre Isidis Utopia Tharsis Elysium Olympus Valles Marineris Noachis Noachian Hesperian Amazonian Jezero Gale
Gusev Meridiani Endeavour Mawrth Vallis Syrtis Nili Fossae Terra Sirenum Cimmeria Tyrrhena Arabia Acidalia Chryse
Planitia Planum Margaritifer Aram Holden Eberswalde Oxia Aeolis Mons Terby Iani Huygens Antoniadi Hellas's Argyre's
Atacama Iceland Icelandic Antarctica Antarctic Mojave Hawaii Hawaiian Kilauea Arizona Utah Nevada California Chile
Chilean Australia Pilbara Greenland Svalbard Rio Tinto Andes Himalaya Tibet Tibetan Sahara
Perseverance Curiosity Opportunity Spirit Viking Pathfinder Phoenix InSight Zhurong Tianwen Apollo Luna Cassini
Galileo Juno Dawn Rosetta Hayabusa Hayabusa2 Clementine Kaguya Chandrayaan Mastcam ChemCam SuperCam Diviner Ingenuity
Messenger BepiColombo Odyssey Express Reconnaissance Orbiter Lunar Prospector Chang'e Artemis Gaussian Fourier
Bayesian Markov Monte Carlo Raman Mossbauer Mössbauer Turing Gray Scott Newtonian Keplerian Lambertian Hapke
""".split())

_TYPE_BIBTEX = {
    "article-journal": "article", "article-magazine": "article", "article-newspaper": "article", "review": "article",
    "book": "book", "chapter": "incollection", "paper-conference": "inproceedings", "thesis": "phdthesis",
    "report": "techreport", "manuscript": "unpublished", "entry-encyclopedia": "incollection",
    "entry-dictionary": "incollection", "entry": "incollection",
}
_TYPE_BIBLATEX = {
    "article-journal": "article", "article-magazine": "article", "article-newspaper": "article", "review": "article",
    "book": "book", "chapter": "incollection", "paper-conference": "inproceedings", "thesis": "thesis",
    "report": "report", "manuscript": "unpublished", "entry-encyclopedia": "inreference",
    "entry-dictionary": "inreference", "entry": "inreference", "article": "online", "webpage": "online",
    "post-weblog": "online", "post": "online", "dataset": "dataset", "software": "software", "patent": "patent",
}
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

_SPECIAL = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "~": r"\textasciitilde{}",
            "^": r"\textasciicircum{}", "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}"}
_COMBINING = {"́": "'", "̀": "`", "̂": "^", "̈": '"', "̃": "~", "̧": "c", "̌": "v",
              "̊": "r", "̄": "=", "̆": "u", "̇": ".", "̋": "H", "̨": "k"}
_LETTERS = {"ß": r"{\ss}", "æ": r"{\ae}", "Æ": r"{\AE}", "ø": r"{\o}", "Ø": r"{\O}", "ł": r"{\l}", "Ł": r"{\L}",
            "œ": r"{\oe}", "Œ": r"{\OE}", "ı": r"{\i}", "đ": r"{\dj}", "–": "--", "—": "---", "‘": "`", "’": "'",
            "“": "``", "”": "''", "…": r"\ldots{}", "°": r"\textdegree{}", "×": r"$\times$", "±": r"$\pm$",
            "µ": r"$\mu$", "μ": r"$\mu$", "α": r"$\alpha$", "β": r"$\beta$", "γ": r"$\gamma$", "δ": r"$\delta$",
            "Δ": r"$\Delta$", "λ": r"$\lambda$", "σ": r"$\sigma$", "θ": r"$\theta$", "ε": r"$\epsilon$", "≈": r"$\approx$",
            "≥": r"$\geq$", "≤": r"$\leq$", " ": "~"}


def tex_escape(s: str, ascii_only: bool = False) -> str:
    out = []
    for ch in s:
        if ch in _SPECIAL:
            out.append(_SPECIAL[ch])
        elif ascii_only and ord(ch) > 127:
            out.append(_ascii_char(ch))
        else:
            out.append(ch)
    return "".join(out)


def _ascii_char(ch: str) -> str:
    if ch in _LETTERS:
        return _LETTERS[ch]
    d = unicodedata.normalize("NFD", ch)
    if len(d) == 2 and d[1] in _COMBINING:
        base, acc = d[0], _COMBINING[d[1]]
        if base == "i" and acc in "'`^\"":
            base = r"\i"
        sep = "" if acc in "'`^\"~=." else " "
        return "{\\" + acc + sep + base + "}"
    return fold(ch) or ch


def _markup_to_tex(s: str, ascii_only: bool) -> str:
    """Escape text and convert CSL inline HTML (<i>, <b>, <sup>, <sub>, <sc>) to LaTeX."""
    tags = {"i": r"\textit{", "b": r"\textbf{", "sup": r"\textsuperscript{", "sub": r"\textsubscript{", "sc": r"\textsc{"}
    parts = re.split(r"(</?(?:i|b|sup|sub|sc)>)", s)
    out = []
    for p in parts:
        m = re.fullmatch(r"<(/?)(i|b|sup|sub|sc)>", p)
        if m:
            out.append("}" if m.group(1) else tags[m.group(2)])
        else:
            out.append(tex_escape(re.sub(r"<[^>]+>", "", p), ascii_only))
    res = "".join(out)
    return res if res.count("{") == res.count("}") else tex_escape(re.sub(r"<[^>]+>", "", s), ascii_only)


def protect_title(title: str, extra: Iterable[str] = (), ascii_only: bool = False) -> str:
    """Brace-protect acronyms and proper nouns so BibTeX styles don't lowercase them."""
    protect = PROTECT_DEFAULT | set(extra or ())
    protect_low = {w.lower() for w in protect}
    words = re.findall(r"[A-Za-z][\w'’-]*", re.sub(r"<[^>]+>", " ", title))
    significant = [w for w in words[1:] if len(w) > 3]
    title_case = bool(significant) and sum(w[0].isupper() for w in significant) / len(significant) > 0.6
    tex = _markup_to_tex(title, ascii_only)
    first_word_done = [False]

    def fix(m: re.Match) -> str:
        w = m.group(0)
        start = m.start()
        prev = tex[:start].rstrip()
        sentence_start = not first_word_done[0] or prev.endswith((":", ".", "?", "!", "--"))
        first_word_done[0] = True
        core = w.strip("'’")
        pieces = [p for p in re.split(r"[-'’]", core) if p]
        should = False
        if any(sum(c.isupper() for c in p) >= 2 for p in pieces):
            should = True  # acronyms and mixed case: CRISM, HiRISE, McSween, OSIRIS-REx
        elif re.search(r"[A-Z].*\d|\d.*[A-Z]", core):
            should = True  # SiO2, H2O, Fe3+
        elif core[:1].isupper() and core.lower() in protect_low:
            should = True
        elif core[:1].isupper() and not title_case and not sentence_start:
            should = True  # proper noun inside a sentence-case title
        elif "-" in core and any(p[:1].isupper() and p.lower() in protect_low for p in core.split("-")):
            should = True
        return "{" + w + "}" if should else w

    # Only touch words outside LaTeX commands.
    return re.sub(r"(?<![\\{\w])[A-Za-z][\w'’]*(?:-[\w'’]+)*", fix, tex)


def _name_bib(n: dict, ascii_only: bool) -> str:
    if is_etal_name(n):
        return "others"  # BibTeX's own "and others" -> et al.
    if n.get("literal"):
        return "{" + tex_escape(n["literal"], ascii_only) + "}"
    fam = " ".join(x for x in [n.get("non-dropping-particle"), n.get("family")] if x)
    fam_t = tex_escape(fam, ascii_only)
    if " " in fam and not re.match(r"^[a-z]", fam):  # multi-word family names: keep together
        fam_t = "{" + fam_t + "}"
    parts = [fam_t]
    if n.get("suffix"):
        parts.append(tex_escape(n["suffix"], ascii_only))
    given = " ".join(x for x in [n.get("given"), n.get("dropping-particle")] if x)
    if given:
        parts.append(tex_escape(given, ascii_only))
    return ", ".join(parts)


def _names(lst, ascii_only) -> str:
    return " and ".join(_name_bib(n, ascii_only) for n in lst if n)


def bib_entry(key: str, csl: dict, *, flavor: str = "bibtex", ascii_only: bool = False,
              protected: Iterable[str] = (), include_abstract: bool = False) -> str:
    t = csl.get("type", "document")
    biblatex = flavor == "biblatex"
    etype = (_TYPE_BIBLATEX if biblatex else _TYPE_BIBTEX).get(t, "misc")
    genre = (csl.get("genre") or "").lower()
    if t == "thesis" and not biblatex and "master" in genre:
        etype = "mastersthesis"
    f: list[tuple[str, str]] = []

    def add(name, value, raw=False):
        if value not in (None, "", []):
            f.append((name, value if raw else tex_escape(str(value), ascii_only)))

    if csl.get("author"):
        add("author", _names(csl["author"], ascii_only), raw=True)
    if csl.get("editor"):
        add("editor", _names(csl["editor"], ascii_only), raw=True)
    if csl.get("title"):
        add("title", protect_title(csl["title"], protected, ascii_only), raw=True)
    cont = csl.get("container-title")
    if cont:
        cont_tex = protect_title(cont, protected, ascii_only)
        if etype == "article":
            add("journaltitle" if biblatex else "journal", cont_tex, raw=True)
        elif etype in ("incollection", "inproceedings", "inreference"):
            add("booktitle", cont_tex, raw=True)
        elif etype in ("online", "misc") and t in ("webpage", "post-weblog", "post"):
            add("organization" if biblatex else "howpublished", cont_tex, raw=True)
        else:
            add("booktitle" if t == "chapter" else ("journaltitle" if biblatex else "journal"), cont_tex, raw=True)
    if csl.get("collection-title"):
        add("series", csl["collection-title"])
    if csl.get("event-title") and etype == "inproceedings":
        add("eventtitle" if biblatex else "organization", csl["event-title"])
    add("volume", csl.get("volume"))
    if etype == "techreport" or etype == "report":
        add("number", csl.get("number"))
        add("type" if not biblatex else "type", csl.get("genre"))
    else:
        add("number", csl.get("issue"))
    if csl.get("page"):
        add("pages", re.sub(r"\s*-+\s*", "--", str(csl["page"])), raw=False)
    pub = csl.get("publisher")
    if pub:
        if etype in ("phdthesis", "mastersthesis") or (biblatex and etype == "thesis"):
            add("school" if not biblatex else "institution", pub)
        elif etype in ("techreport", "report"):
            add("institution", pub)
        else:
            add("publisher", pub)
    if biblatex and etype == "thesis":
        add("type", "phdthesis" if "master" not in genre else "mathesis", raw=True)
    add("location" if biblatex else "address", csl.get("publisher-place"))
    add("edition", csl.get("edition"))
    dp = date_parts(csl, "issued")
    if dp:
        if biblatex:
            add("date", "-".join(f"{p:04d}" if i == 0 else f"{p:02d}" for i, p in enumerate(dp)), raw=True)
        else:
            add("year", str(dp[0]), raw=True)
            if len(dp) > 1 and 1 <= dp[1] <= 12:
                f.append(("month", _MONTHS[dp[1] - 1]))  # unbraced macro
    elif isinstance(csl.get("issued"), dict) and csl["issued"].get("literal"):
        add("year" if not biblatex else "date", csl["issued"]["literal"])
    aid = normalize_arxiv(csl.get("number") or "") if "arxiv" in (csl.get("number") or "").lower() else None
    if not aid and str(csl.get("DOI", "")).lower().startswith("10.48550/arxiv."):
        aid = normalize_arxiv(csl["DOI"])
    if aid:
        add("eprint", aid, raw=True)
        if biblatex:
            add("eprinttype", "arxiv", raw=True)
        else:
            add("archivePrefix", "arXiv", raw=True)
    if csl.get("DOI"):
        add("doi", csl["DOI"], raw=True)
    url = csl.get("URL")
    if url and (not csl.get("DOI") or t in ("webpage", "software", "dataset", "post-weblog")):
        if biblatex or etype != "misc":
            add("url", url, raw=True)
        else:
            add("howpublished", "\\url{" + url + "}", raw=True)
        acc = date_parts(csl, "accessed")
        if acc:
            stamp = "-".join(f"{p:04d}" if i == 0 else f"{p:02d}" for i, p in enumerate(acc))
            if biblatex:
                add("urldate", stamp, raw=True)
            else:
                add("note", f"Accessed {stamp}")
    add("isbn", csl.get("ISBN"), raw=True)
    add("issn", csl.get("ISSN"), raw=True)
    if t in ("software", "dataset") and csl.get("version"):
        add("version", csl["version"])
    if t == "article" and not biblatex and csl.get("publisher") and csl.get("publisher") != "arXiv":
        add("note", f"Preprint, {csl['publisher']}")
    if include_abstract and csl.get("abstract"):
        add("abstract", csl["abstract"])
    # De-duplicate field names (keep first).
    seen, lines = set(), []
    for name, value in f:
        if name in seen:
            continue
        seen.add(name)
        if name == "month" and not biblatex:
            lines.append(f"  {name} = {value}")
        else:
            lines.append(f"  {name} = {{{value}}}")
    return f"@{etype}{{{key},\n" + ",\n".join(lines) + "\n}\n"


def to_bibtex(items: Iterable[dict], *, flavor: str = "bibtex", ascii_only: bool = False,
              protected: Iterable[str] = (), include_abstract: bool = False, header: bool = True) -> str:
    items = list(items)
    out = []
    if header:
        out.append(f"% Exported by Rumil ({len(items)} entries). Edit references in Rumil, not here: this file is regenerated.\n")
    for it in items:
        out.append(bib_entry(it["citekey"], it["csl"], flavor=flavor, ascii_only=ascii_only, protected=protected,
                             include_abstract=include_abstract))
    return "\n".join(out)
