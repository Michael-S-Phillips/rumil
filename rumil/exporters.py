"""CSL-JSON and RIS export."""

from __future__ import annotations

import json
from typing import Iterable

from .csl import date_parts
from .textutil import strip_markup


def to_csljson(items: Iterable[dict], indent: int = 1) -> str:
    out = []
    for it in items:
        rec = {"id": it["citekey"], "citation-key": it["citekey"]}
        rec.update(it["csl"])
        out.append(rec)
    return json.dumps(out, ensure_ascii=False, indent=indent) + "\n"


_RIS_TYPES = {"article-journal": "JOUR", "book": "BOOK", "chapter": "CHAP", "paper-conference": "CPAPER",
              "thesis": "THES", "report": "RPRT", "webpage": "ELEC", "dataset": "DATA", "software": "COMP",
              "article": "JOUR", "manuscript": "UNPB", "map": "MAP", "patent": "PAT", "speech": "SLIDE",
              "article-magazine": "MGZN", "article-newspaper": "NEWS", "entry-encyclopedia": "ENCYC"}


def to_ris(items: Iterable[dict]) -> str:
    lines = []
    for it in items:
        c = it["csl"]
        lines.append(f"TY  - {_RIS_TYPES.get(c.get('type'), 'GEN')}")
        lines.append(f"ID  - {it['citekey']}")
        for n in c.get("author", []):
            lines.append("AU  - " + (n.get("literal") or ", ".join(x for x in [n.get("family"), n.get("given")] if x)))
        for n in c.get("editor", []):
            lines.append("ED  - " + (n.get("literal") or ", ".join(x for x in [n.get("family"), n.get("given")] if x)))
        if c.get("title"):
            lines.append("TI  - " + strip_markup(c["title"]))
        if c.get("container-title"):
            lines.append(("T2  - " if c.get("type") != "article-journal" else "JO  - ") + strip_markup(c["container-title"]))
        dp = date_parts(c, "issued")
        if dp:
            lines.append(f"PY  - {dp[0]}")
            lines.append("DA  - " + "/".join(f"{p:02d}" if i else str(p) for i, p in enumerate(dp)) + ("/" * (3 - len(dp))))
        for k, tag in (("volume", "VL"), ("issue", "IS"), ("publisher", "PB"), ("publisher-place", "CY"),
                       ("DOI", "DO"), ("URL", "UR"), ("ISSN", "SN"), ("ISBN", "SN"), ("abstract", "AB"), ("note", "N1")):
            if c.get(k):
                lines.append(f"{tag}  - {strip_markup(str(c[k])) if k != 'URL' else c[k]}")
        if c.get("page"):
            sp, _, ep = str(c["page"]).partition("-")
            lines.append(f"SP  - {sp}")
            if ep:
                lines.append(f"EP  - {ep}")
        for t in it.get("tags", []) or []:
            lines.append(f"KW  - {t}")
        lines.append("ER  - ")
        lines.append("")
    return "\n".join(lines)
