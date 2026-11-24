"""One object that wires the library, registries, verification, and formatting together.

The MCP server (for Claude), the web app (library window), and the CLI all use this.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .csl import clean_item, summary, today_date, year_of
from .db import Library, NotFound
from .exporters import to_csljson, to_ris
from .ids import normalize_doi
from .bibtex import to_bibtex
from .formatting import Formatter, StyleRegistry
from .http import Http, HttpError
from .jobs import JobManager
from .resolvers import Resolver, pdf_text
from .textutil import fold, strip_markup, title_in_text
from .verify import Verifier, describe_status


class Rumil:
    def __init__(self, cfg: Config | None = None, http: Http | None = None):
        self.cfg = cfg or Config()
        self.cfg.home.mkdir(parents=True, exist_ok=True)
        self.http = http or Http(self.cfg.get("contact_email", ""))
        self.lib = Library(self.cfg)
        self.resolver = Resolver(self.http, self.cfg.get("ads_token", ""))
        self.verifier = Verifier(self.lib, self.resolver)
        self.styles = StyleRegistry(self.cfg, self.http)
        self.formatter = Formatter(self.lib, self.styles)
        self.jobs = JobManager()
        self._export_timer: threading.Timer | None = None
        self._export_lock = threading.Lock()
        try:
            self.lib.backup_if_due()
        except Exception:
            pass

    # ------------------------------------------------------------ housekeeping
    def changed(self, delay: float = 1.5) -> None:
        """Schedule a (debounced) rewrite of library.bib / library.json."""
        with self._export_lock:
            if self._export_timer:
                self._export_timer.cancel()
            self._export_timer = threading.Timer(delay, self._export_now)
            self._export_timer.daemon = True
            self._export_timer.start()

    def _export_now(self) -> None:
        try:
            self.lib.auto_export()
        except Exception:
            pass
        try:
            self.lib.backup_if_due()  # long-running servers still get one snapshot per day
        except Exception:
            pass

    def flush(self) -> None:
        with self._export_lock:
            if self._export_timer:
                self._export_timer.cancel()
                self._export_timer = None
        self._export_now()

    # -------------------------------------------------------------- adding
    def add(self, identifier: str, *, collection: str | None = None, tags: Iterable[str] = (), citekey: str | None = None,
            download_pdf: bool = True, actor: str = "user") -> dict:
        """Add a reference by DOI, arXiv id, ISBN, PMID, ADS bibcode, or URL. Metadata comes from the registry."""
        identifier = (identifier or "").strip()
        if not identifier:
            raise ValueError("Give a DOI, arXiv id, ISBN, PMID, ADS bibcode, or URL")
        existing = self.lib.get(identifier.lstrip("@"), full=False)
        if existing and not re.search(r"[/.:]", identifier):
            return {"status": "exists", "item": self._item_out(existing), "message": f"Already in library as @{existing['citekey']}"}
        reg = self.resolver.resolve(identifier)
        dups = self.lib.find_duplicates_of(reg.csl, strong_only=True)
        if dups:
            d = dups[0][0]
            if collection:
                self.lib.add_to_collection(collection, [d["id"]])
            if tags:
                self.lib.add_tags([d["id"]], list(tags))
            self.changed()
            return {"status": "exists", "item": self._item_out(self.lib.get(d["id"])),
                    "message": f"Already in library as @{d['citekey']} ({dups[0][1]})", "warnings": reg.warnings}
        csl = dict(reg.csl)
        given = normalize_doi(identifier)
        if given and csl.get("DOI") and given.lower() == csl["DOI"].lower():
            csl["DOI"] = given  # keep the publisher's capitals ('...<0987:HSESAT>2.0.CO;2'); Crossref lowercases some
        if not reg.authoritative and not csl.get("accessed"):
            csl["accessed"] = today_date()
        item = self.lib.add_item(csl, source=reg.source, citekey=citekey, collections=[collection] if collection else [],
                                 tags=tags, actor=actor)
        vr = self.verifier.verify_with(item, reg)
        self.lib.set_verification(item["id"], vr.status, vr.details(), vr.suggestions)
        if reg.registry_record.get("bibcode"):
            self.lib.update_item(item["id"], patch={"note": f"ADS bibcode: {reg.registry_record['bibcode']}"}, actor=actor,
                                 reset_verification=False)
        pdf = None
        if download_pdf and reg.pdf_url:
            pdf = self.attach_pdf_from_url(item["id"], reg.pdf_url)
        self.changed()
        out = {"status": "added", "item": self._item_out(self.lib.get(item["id"])), "source": reg.source,
               "warnings": reg.warnings + self._possible_dup_warnings(item)}
        if pdf:
            out["pdf"] = pdf
        return out

    def add_manual(self, csl: dict, *, evidence_url: str | None = None, collection: str | None = None,
                   tags: Iterable[str] = (), citekey: str | None = None, actor: str = "user") -> dict:
        """Add a work that has no identifier (e.g. an LPSC abstract), checked against an evidence URL."""
        csl = clean_item(csl)
        if not csl.get("title"):
            raise ValueError("A manual reference needs at least a title")
        dups = self.lib.find_duplicates_of(csl, strong_only=True)
        if dups:
            d = dups[0][0]
            return {"status": "exists", "item": self._item_out(self.lib.get(d["id"])),
                    "message": f"Already in library as @{d['citekey']} ({dups[0][1]})"}
        status, summary_txt, checks = "unverified", "Entered by hand; no registry record", []
        if evidence_url:
            csl.setdefault("URL", evidence_url)
            try:
                resp = self.http.request(evidence_url, retries=1)
                if resp.ok:
                    body = resp.body
                    text = pdf_text(body, max_pages=3) if (body[:5] == b"%PDF-") else strip_markup(resp.text)
                    t_ok = title_in_text(csl.get("title", ""), text)
                    fam = ((csl.get("author") or [{}])[0].get("family") or "")
                    a_ok = (not fam) or fold(fam) in fold(text)
                    yr = year_of(csl)
                    y_ok = (not yr) or str(yr) in text
                    checks.append({"check": "title_at_evidence_url", "ok": t_ok})
                    checks.append({"check": "first_author_at_evidence_url", "ok": a_ok})
                    checks.append({"check": "year_at_evidence_url", "ok": y_ok})
                    if t_ok and a_ok and y_ok:
                        status, summary_txt = "verified_no_doi", f"Title, first author, and year found at {evidence_url}"
                    else:
                        missing = [n for n, ok in (("the title", t_ok), ("the first author", a_ok), ("the year", y_ok)) if not ok]
                        summary_txt = f"Evidence page does not show {', '.join(missing)}; check the details"
                else:
                    summary_txt = f"Evidence URL returned HTTP {resp.status}"
            except HttpError as e:
                summary_txt = f"Could not fetch evidence URL: {e}"
        item = self.lib.add_item(csl, source="manual", citekey=citekey, collections=[collection] if collection else [],
                                 tags=tags, actor=actor)
        self.lib.set_verification(item["id"], status, {"summary": summary_txt, "checks": checks, "issues": []}, [])
        if status != "verified_no_doi" and csl.get("type") in ("article-journal", "paper-conference", "chapter", "book"):
            # Give the registry a chance (it may have a DOI after all).
            try:
                self.verifier.verify_and_store(item["id"])
            except Exception:
                pass
        self.changed()
        return {"status": "added", "item": self._item_out(self.lib.get(item["id"])),
                "warnings": self._possible_dup_warnings(item)}

    def _possible_dup_warnings(self, item: dict) -> list[str]:
        return [f"Possible duplicate of @{d['citekey']} ({why}); review it in the Duplicates view"
                for d, why, _ in self.lib.find_duplicates_of(item["csl"], exclude_id=item["id"])][:3]

    def attach_pdf_from_url(self, ref: Any, url: str) -> dict | None:
        item = self.lib.require(ref, full=False)
        try:
            resp = self.http.request(url, accept="application/pdf", retries=1, timeout=40)
        except HttpError as e:
            return {"saved": False, "reason": str(e)}
        if not resp.ok or resp.body[:5] != b"%PDF-":
            self.lib.add_attachment(item["id"], title="PDF (publisher link)", url=url, content_type="application/pdf",
                                    link_mode="linked_url")
            return {"saved": False, "reason": "PDF needs a login or subscription; saved the link instead", "url": url}
        folder = self.cfg.storage_dir / item["citekey"]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{item['citekey']}.pdf"
        path.write_bytes(resp.body)
        self.lib.add_attachment(item["id"], title="Full Text PDF", path=str(path), url=url, content_type="application/pdf",
                                link_mode="imported_url")
        return {"saved": True, "path": str(path)}

    # ------------------------------------------------------------- finding
    def find(self, query: str = "", *, title: str = "", author: str = "", year: int | None = None, limit: int = 5,
             registries: bool = True) -> dict:
        """Search the library first, then the registries, for a work to cite."""
        q = " ".join(x for x in [query, title, author, str(year or "")] if x).strip()
        lib_hits, _ = self.lib.search(q, limit=limit) if q else ([], 0)
        if not lib_hits and title:
            lib_hits, _ = self.lib.search(title, limit=limit)
        out = {"in_library": [self._item_out(h, brief=True) for h in lib_hits], "candidates": []}
        if registries:
            try:
                cands = self.resolver.search(query=query, title=title, author=author, year=year, rows=limit)
            except HttpError as e:
                cands = [{"error": str(e)}]
            for c in cands:
                if "error" in c:
                    out.setdefault("errors", []).append(c["error"])
                    continue
                csl = c["csl"]
                dups = self.lib.find_duplicates_of(csl, strong_only=True)
                out["candidates"].append({
                    "title": strip_markup(csl.get("title", "")), "authors": "; ".join(
                        (n.get("family") or n.get("literal") or "") + (", " + n["given"] if n.get("given") else "")
                        for n in csl.get("author", [])[:6]) + (" et al." if len(csl.get("author", [])) > 6 else ""),
                    "year": (csl.get("issued") or {}).get("date-parts", [[None]])[0][0] if isinstance(csl.get("issued"), dict) else None,
                    "container": csl.get("container-title"), "volume": csl.get("volume"), "page": csl.get("page"),
                    "type": csl.get("type"), "DOI": csl.get("DOI"), "source": c["source"],
                    "confidence": c.get("confidence"), "match": c.get("match"),
                    "in_library_as": dups[0][0]["citekey"] if dups else None,
                    "add_with": csl.get("DOI") or (csl.get("URL") if c["source"] == "ads" else None),
                })
        return out

    # ---------------------------------------------------------- verification
    def verify(self, refs: Iterable[Any] | None = None, *, collection: str | None = None, everything: bool = False,
               only_unchecked: bool = False, apply_safe: bool | str = False, background_over: int = 25) -> dict:
        if everything:
            ids = [r[0] for r in self.lib.q("SELECT id FROM items WHERE deleted = 0" +
                                            (" AND verify_status IN ('unchecked','error')" if only_unchecked else ""))]
        elif collection:
            ids = self.lib.collection_item_ids(collection)
        else:
            ids = []
            for r in refs or []:
                iid = self.lib.resolve_id(r)
                if iid is None:
                    raise NotFound(f"No item '{r}' in the library")
                ids.append(iid)
        workers = int(self.cfg.get("verify_concurrency", 3) or 3)
        if len(ids) > background_over:
            def run(job):
                res = self.verifier.verify_many(ids, apply_safe=apply_safe, workers=workers,
                                                progress=lambda d, t, it: job.update(d, t, f"@{it.get('citekey', '')}"),
                                                should_stop=lambda: job.cancel_requested)
                self.changed()
                return res
            job = self.jobs.start(f"Verify {len(ids)} references", run)
            return {"background_job": job.id, "count": len(ids),
                    "message": f"Verifying {len(ids)} references in the background; check progress with the job id"}
        results = []
        for iid in ids:
            try:
                it = self.verifier.verify_and_store(iid, apply_safe=apply_safe)
            except Exception as e:
                self.lib.set_verification(iid, "error", {"summary": str(e)}, [])
                it = self.lib.get(iid)
            results.append(self.verification_out(it))
        self.changed()
        return {"results": results}

    def verification_out(self, it: dict) -> dict:
        d = it.get("verify_details") or {}
        return {"citekey": it["citekey"], "title": strip_markup(it["csl"].get("title", "")), "status": it["verify_status"],
                "meaning": describe_status(it["verify_status"]), "summary": d.get("summary"), "issues": d.get("issues", []),
                "suggestions": [{k: v for k, v in s.items() if k in ("id", "kind", "field", "current", "proposed", "safe", "correction", "keepable", "reason")}
                                for s in it.get("suggestions") or []]}

    def apply_fixes(self, ref: Any, which: str | list[str] = "safe", keep: Iterable[str] = ()) -> dict:
        keep = [k for k in keep or [] if k]
        if keep:
            self.verifier.accept(ref, keep, reverify=False)
        it = self.verifier.apply(ref, which) if which else self.lib.require(ref)
        if keep and not which:
            it = self.verifier.verify_and_store(ref)
        self.changed()
        return self.verification_out(it)

    # ---------------------------------------------------------- import / LaTeX
    def import_bibfile(self, path: str, collection: str | None = None, verify: bool = True) -> dict:
        """Import a .bib/.ris/CSL-JSON file and (by default) check every new entry against the registries."""
        from .importers import import_file
        rep = import_file(self.lib, path, collection=collection)
        keys = [a["citekey"] for a in rep["added"]]
        if verify and keys:
            rep["verification"] = self.verify(keys)
        self.changed()
        return rep

    def check_latex(self, tex_path: str, *, write_bib: str | None = None, biblatex: bool = False,
                    overwrite: bool = False, import_missing: bool = False, collection: str | None = None) -> dict:
        """Audit a .tex file. With import_missing, cited entries found only in the document's own .bib are imported
        (keeping their keys) and verified first, so every citation ends up checked."""
        from .latex import check_tex, import_missing_cited
        imported = None
        if import_missing:
            imported = import_missing_cited(self.lib, tex_path, collection=collection)
            keys = [a["citekey"] for a in imported["added"]]
            if keys and len(keys) <= 40:
                out = self.verify(keys, background_over=10**9)
                imported["verification"] = {"checked": len(keys), "by_status": _count_status(out)}
            elif keys:  # many: check in the background; run check_latex again when the job finishes
                imported["verification"] = self.verify(keys, background_over=0)
            self.changed()
        rep = check_tex(self.lib, tex_path, write_bib=write_bib, flavor="biblatex" if biblatex else "bibtex",
                        ascii_only=bool(self.cfg.get("bibtex_ascii")), protected=self.cfg.get("protected_words") or [],
                        overwrite=overwrite)
        if imported is not None:
            rep["imported"] = {k: imported[k] for k in ("added", "already_in_library", "aliases_added", "key_conflicts",
                                                         "still_missing", "verification") if k in imported}
        return rep

    # --------------------------------------------------------------- export
    def items_for(self, keys: Iterable[str] | None = None, collection: str | None = None) -> list[dict]:
        if keys:
            out = []
            for k in keys:
                it = self.lib.get(k, full=True)
                if not it:
                    raise NotFound(f"No item '{k}' in the library")
                out.append(it)
            return out
        if collection:
            return list(self.lib.iter_items(ids=self.lib.collection_item_ids(collection)))
        return list(self.lib.iter_items())

    def export(self, fmt: str = "bibtex", keys: Iterable[str] | None = None, collection: str | None = None,
               path: str | None = None) -> dict:
        items = self.items_for(keys, collection)
        fmt = fmt.lower()
        if fmt in ("bibtex", "bib", "biblatex"):
            text = to_bibtex(items, flavor="biblatex" if fmt == "biblatex" else "bibtex",
                             ascii_only=bool(self.cfg.get("bibtex_ascii")), protected=self.cfg.get("protected_words") or [])
            ext = ".bib"
        elif fmt in ("csljson", "json", "csl-json"):
            text, ext = to_csljson(items), ".json"
        elif fmt == "ris":
            text, ext = to_ris(items), ".ris"
        else:
            raise ValueError("format must be bibtex, biblatex, csljson, or ris")
        out = {"format": fmt, "count": len(items)}
        if path:
            p = Path(path).expanduser()
            if p.is_dir():
                p = p / f"references{ext}"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            out["path"] = str(p)
        else:
            out["text"] = text
        return out

    # ---------------------------------------------------------------- views
    def _item_out(self, it: dict, brief: bool = False) -> dict:
        s = summary(it)
        if brief:
            return s
        d = it.get("verify_details") or {}
        s.update({"verification": d.get("summary"), "meaning": describe_status(it["verify_status"]),
                  "aliases": it.get("aliases", []), "collections": [c["path"] for c in it.get("collections", [])],
                  "tags": it.get("tags", [])})
        return s

    def item_detail(self, ref: Any, include_abstract: bool = True, include_notes: bool = True) -> dict:
        it = self.lib.require(ref)
        csl = dict(it["csl"])
        if not include_abstract:
            csl.pop("abstract", None)
        out = {"citekey": it["citekey"], "aliases": it["aliases"], "csl": csl, "status": it["verify_status"],
               "meaning": describe_status(it["verify_status"]), "verification": it.get("verify_details"),
               "suggestions": it.get("suggestions"), "collections": [c["path"] for c in it["collections"]],
               "tags": it["tags"], "attachments": [{"title": a["title"], "path": a["path"], "url": a["url"],
                                                    "has_fulltext": bool(a.get("fulltext_path") or (a.get("path") or "").lower().endswith(".pdf"))}
                                                   for a in it["attachments"]],
               "date_added": it["date_added"], "source": it["source"]}
        if include_notes:
            out["notes"] = [strip_markup(n["html"])[:3000] for n in it["notes"]]
        return out


def _count_status(verify_out: dict) -> dict:
    counts: dict[str, int] = {}
    for r in verify_out.get("results", []):
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts
