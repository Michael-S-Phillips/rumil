"""Read the full text of a reference's attachments, so claims can be checked against the paper itself."""

from __future__ import annotations

import re
from pathlib import Path

from .db import Library
from .textutil import strip_markup


def item_fulltext(lib: Library, ref, *, max_chars: int = 20000, offset: int = 0, query: str | None = None) -> dict:
    item = lib.require(ref)
    atts = item.get("attachments") or []
    sources = []
    text = ""
    for a in atts:
        ft = a.get("fulltext_path")
        if ft and Path(ft).exists():
            text = Path(ft).read_text(encoding="utf-8", errors="replace")
            sources.append({"attachment": a.get("title"), "from": "Zotero full-text index"})
            break
    if not text:
        for a in atts:
            p = a.get("path")
            if not p or not Path(p).exists():
                continue
            if (a.get("content_type") or "").endswith("pdf") or p.lower().endswith(".pdf"):
                text = _pdf_text(Path(p))
                if text:
                    sources.append({"attachment": a.get("title"), "from": "PDF text extraction"})
                    break
            elif (a.get("content_type") or "").startswith("text/html") or p.lower().endswith((".html", ".htm")):
                text = strip_markup(re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", Path(p).read_text(errors="replace"), flags=re.S | re.I))
                if text:
                    sources.append({"attachment": a.get("title"), "from": "saved web page"})
                    break
    if not text:
        abstract = item["csl"].get("abstract")
        return {"citekey": item["citekey"], "available": False, "abstract": abstract,
                "message": "No readable attachment for this item" + ("; only the abstract is available" if abstract else "")}
    result = {"citekey": item["citekey"], "available": True, "source": sources, "total_chars": len(text)}
    if query:
        hits = []
        terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
        low = text.lower()
        for m in re.finditer(re.escape(terms[0]) if terms else r"$^", low):
            window = low[max(0, m.start() - 400): m.end() + 400]
            if all(t in window for t in terms):
                hits.append({"offset": m.start(), "excerpt": text[max(0, m.start() - 400): m.end() + 400].strip()})
            if len(hits) >= 8:
                break
        result["query"] = query
        result["matches"] = hits
        return result
    result["offset"] = offset
    result["text"] = text[offset: offset + max_chars]
    result["truncated"] = offset + max_chars < len(text)
    return result


def _pdf_text(path: Path, max_pages: int = 80) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n".join((reader.pages[i].extract_text() or "") for i in range(min(max_pages, len(reader.pages))))
    except Exception:
        return ""
