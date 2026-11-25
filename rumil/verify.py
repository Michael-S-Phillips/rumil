"""Verification: check every reference against the registry that issued it.

Statuses (stored on each item):
    verified         registry record found and title/author/year agree
    verified_no_doi  no DOI, but the work was found and matched (arXiv, ISBN, ADS)
    url_ok           web page / software / dataset without DOI whose URL responds
    differs          the same work as the registry record, but the title wording, author names, volume,
                     issue, or pages differ (registry corrections are offered)
    mismatch         the registry record looks like a different work (title or year disagree)
    doi_found        no DOI locally, but a confident registry match exists (suggests the DOI)
    unverified       nothing could confirm this work exists as described
    url_dead         the only link does not respond
    retracted        the work has been retracted or withdrawn
    doi_invalid      the DOI is not registered anywhere
    error            a network error prevented checking (try again)
    unchecked        not yet checked

Suggestions are proposed fixes. "Safe" ones only fill empty fields from the
registry record or remove junk, so they can be applied without review.
"Corrections" replace a field with the registry's value when the work is
otherwise confirmed (same DOI, title, and year); apply them with
apply(..., "corrections") or keep your value with accept(..., fields).
"""

from __future__ import annotations

import concurrent.futures as cf
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from .csl import _ORG_WORDS, _split_suffix, creators, family_of, is_etal_name, is_name_suffix, parse_name, year_of
from .db import Library, NotFound
from .http import HttpError
from .ids import doi_key, is_junk_url, normalize_arxiv, normalize_doi
from .journals import journal_from_macro
from .resolvers import NotFoundError, Resolution, Resolver, is_agu_manuscript_number, is_notice_title, lpsc_info
from .textutil import (ascii_fold, family_matches, fold, name_key, normalize_punct, split_running_head, strip_database_tags, strip_markup,
                       title_in_text, title_similarity, title_words)

FILLABLE = ["container-title", "volume", "issue", "page", "ISSN", "ISBN", "publisher", "publisher-place",
            "abstract", "issued", "author", "editor", "container-title-short", "event-title", "number"]
COMPARED = ["volume", "issue", "page"]
SEARCH_FILLABLE = ["container-title", "container-title-short", "volume", "issue", "page", "ISSN", "event-title"]
SCHOLARLY_TYPES = {"article-journal", "paper-conference", "chapter", "book", "report", "thesis", "article",
                   "entry-encyclopedia", "entry", "review", "manuscript", "document"}
WEB_TYPES = {"webpage", "software", "dataset", "post-weblog", "post", "map", "graphic", "motion_picture", "broadcast"}
STATUS_RANK = ["retracted", "doi_invalid", "mismatch", "differs", "doi_found", "url_dead", "unverified", "error",
               "verified", "verified_no_doi", "url_ok"]
CATALOG_SOURCES = ("openlibrary", "googlebooks")  # library catalogs: titles/authors are less authoritative


@dataclass
class VerifyResult:
    status: str
    summary: str
    checks: list[dict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    suggestions: list[dict] = field(default_factory=list)
    registry: dict | None = None  # {"source": ..., "csl": {...}, "extras": {...}}

    def details(self) -> dict:
        d = asdict(self)
        d.pop("suggestions")
        d.pop("status")
        return d


class Verifier:
    def __init__(self, lib: Library, resolver: Resolver):
        self.lib = lib
        self.resolver = resolver

    # ------------------------------------------------------------ public API
    def verify(self, item: dict) -> VerifyResult:
        csl = item["csl"]
        acc = item.get("accepted") or {}
        res = VerifyResult(status="unverified", summary="")
        self._local_checks(item, res)
        try:
            if csl.get("DOI"):
                self._verify_doi(csl, res, acc)
            elif item.get("arxiv") or _arxiv_from(csl):
                self._verify_arxiv(csl, res, item.get("arxiv") or _arxiv_from(csl), acc)
            elif csl.get("ISBN") and csl.get("type") == "book":
                self._verify_isbn(csl, res, acc)
            elif csl.get("type") in WEB_TYPES:
                self._verify_url(csl, res)
            else:
                self._verify_by_search(csl, res)
        except HttpError as e:
            res.status = "error"
            res.summary = f"Could not reach the registry: {e}"
        _suffix_repair(csl, res)
        self._duplicate_check(item, res)
        _note_pending_fixes(res)
        _number_suggestions(res)
        return res

    def verify_and_store(self, ref: Any, apply_safe: bool | str = False) -> dict:
        """Verify and save the result. apply_safe: False, True/'safe' (fill gaps, remove junk), or 'corrections'
        (also replace title/author/volume/issue/page with the registry's when the work is otherwise confirmed)."""
        item = self.lib.require(ref)
        res = self.verify(item)
        self.lib.set_verification(item["id"], res.status, res.details(), res.suggestions)
        mode = "safe" if apply_safe is True else (apply_safe or None)
        if mode and any(s.get("safe") or (mode == "corrections" and s.get("correction")) for s in res.suggestions):
            self.apply(item["id"], mode, reverify=True)
        return self.lib.require(item["id"])

    def verify_many(self, refs: Iterable[Any], apply_safe: bool | str = False, workers: int = 3,
                    progress: Callable[[int, int, dict], None] | None = None,
                    should_stop: Callable[[], bool] | None = None) -> dict:
        ids = [self.lib.resolve_id(r) for r in refs]
        ids = [i for i in ids if i is not None]
        counts: dict[str, int] = {}
        done = 0
        with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(self._safe_verify_store, i, apply_safe): i for i in ids}
            for fut in cf.as_completed(futs):
                item = fut.result()
                done += 1
                st = item.get("verify_status", "error") if item else "error"
                counts[st] = counts.get(st, 0) + 1
                if progress:
                    progress(done, len(ids), item or {})
                if should_stop and should_stop():
                    for f in futs:
                        f.cancel()
                    break
        return {"checked": done, "total": len(ids), "by_status": counts}

    def _safe_verify_store(self, iid: int, apply_safe: bool | str) -> dict | None:
        try:
            return self.verify_and_store(iid, apply_safe=apply_safe)
        except NotFound:
            return None
        except Exception as e:  # never let one bad record stop a batch
            self.lib.set_verification(iid, "error", {"summary": f"{type(e).__name__}: {e}"}, [])
            return self.lib.get(iid)

    def apply(self, ref: Any, which: str | Iterable[str] = "safe", reverify: bool = True, _round: int = 0) -> dict:
        """Apply stored suggestions: 'safe', 'corrections' (safe + registry corrections), 'all', or ids like ['s1']."""
        item = self.lib.require(ref)
        sugg = item.get("suggestions") or []
        if which == "safe":
            chosen = [s for s in sugg if s.get("safe")]
        elif which == "corrections":
            chosen = [s for s in sugg if s.get("safe") or s.get("correction")]
        elif which == "all":
            chosen = [s for s in sugg if s["kind"] != "merge_duplicate"]
        else:
            wanted = set(which)
            chosen = [s for s in sugg if s.get("id") in wanted]
        if not chosen:
            return item
        patch: dict = {}
        applied = []
        for s in chosen:
            k = s["kind"]
            if k in ("set_field", "clean_field"):
                patch[s["field"]] = s.get("proposed")
            elif k in ("add_doi", "replace_doi"):
                patch["DOI"] = s["proposed"]
                for fk, fv in (s.get("fill") or {}).items():
                    if fk not in item["csl"] or k == "replace_doi":
                        patch[fk] = fv
            elif k == "replace_metadata":
                patch.update(s.get("proposed") or {})
            elif k == "published_version":
                pub = self.resolver.resolve_doi(s["proposed"])
                if title_similarity(item["csl"].get("title"), pub.csl.get("title")) < 0.7:
                    raise ValueError(f"The published record's title ('{strip_markup(pub.csl.get('title'))[:100]}') differs "
                                     "from the preprint's; check it is the same work and update the item by hand")
                note = item["csl"].get("note", "")
                pre = item["csl"].get("DOI") or item["csl"].get("number") or ""
                new = dict(pub.csl)
                new["note"] = (note + "\n" if note else "") + f"Preprint version: {pre}".strip()
                self.lib.update_item(item["id"], csl=new, actor="rumil-verify",
                                     reason=f"replaced preprint with published version {s['proposed']}")
                applied.append(s["id"])
                continue
            elif k == "merge_duplicate":
                self.lib.merge(item["id"], [s["proposed"]], actor="rumil-verify")
                applied.append(s["id"])
                continue
            applied.append(s["id"])
        if patch:
            self.lib.update_item(item["id"], patch=patch, actor="rumil-verify",
                                 reason="applied verification suggestions " + ", ".join(applied))
        if reverify:
            out = self.verify_and_store(item["id"])
            # A fix can reveal another one (once a DOI is added, the registry record shows the full page range):
            # apply those too, a couple of rounds at most.
            if which in ("safe", "corrections") and _round < 2 and patch and any(
                    sg.get("safe") or (which == "corrections" and sg.get("correction")) for sg in out.get("suggestions") or []):
                return self.apply(item["id"], which, reverify=True, _round=_round + 1)
            return out
        return self.lib.require(item["id"])

    def accept(self, ref: Any, fields: Iterable[str], reverify: bool = True) -> dict:
        """Keep the library's value for these fields even though the registry differs (e.g. a deliberate title form)."""
        item = self.lib.require(ref)
        reg = ((item.get("verify_details") or {}).get("registry") or {}).get("csl") or {}
        acc = dict(item.get("accepted") or {})
        if not reg:
            raise ValueError("The item has no registry record yet; verify the item first")
        for f in fields:
            # A field the registry record lacks (Crossref records with no title, for one) is kept as None:
            # "the registry has nothing here", so the library's value stands until the registry gains one.
            acc[f] = reg.get(f)
        self.lib.set_accepted(item["id"], acc)
        if reverify:
            return self.verify_and_store(item["id"])
        return self.lib.require(item["id"])

    # ---------------------------------------------------------- local checks
    def _local_checks(self, item: dict, res: VerifyResult) -> None:
        csl = item["csl"]
        t = csl.get("type")
        if not csl.get("title"):
            res.issues.append("Missing title")
        if not creators(csl) and t not in ("webpage",):
            res.issues.append("Missing authors")
        if not year_of(csl) and t not in ("webpage", "software"):
            res.issues.append("Missing year")
        if t == "article-journal" and not csl.get("container-title"):
            res.issues.append("Journal article without a journal name")
        url = csl.get("URL")
        if url and is_junk_url(url):
            res.issues.append(f"Unusable URL '{url[:60]}'")
            res.suggestions.append({"kind": "clean_field", "field": "URL", "current": url, "proposed": None,
                                    "safe": True, "reason": "Placeholder link from a database export, not a web address"})
        note = csl.get("note") or ""
        cleaned = "\n".join(l for l in note.splitlines() if not re.match(r"^\s*Type: [A-Z][A-Za-z ]+$", l)).strip()
        if note and cleaned != note.strip():
            res.suggestions.append({"kind": "clean_field", "field": "note", "current": note, "proposed": cleaned or None,
                                    "safe": True, "reason": "Import residue ('Type: Journal Article') in the note field"})
        if any(is_etal_name(c) for c in creators(csl)):
            res.issues.append("Author list is truncated ('et al.' / 'others' stands in for real names)")
        cont = csl.get("container-title") or ""
        if cont.startswith("\\"):
            full = journal_from_macro(cont)
            res.issues.append(f"Journal name is a LaTeX macro ('{cont}')")
            if full:
                res.suggestions.append({"kind": "set_field", "field": "container-title", "current": cont, "proposed": full,
                                        "safe": True, "reason": f"AAS/ADS journal macro {cont} stands for '{full}'"})

    # --------------------------------------------------------------- DOI path
    def _verify_doi(self, csl: dict, res: VerifyResult, acc: dict | None = None) -> None:
        doi = normalize_doi(csl["DOI"])
        if not doi:
            res.status, res.summary = "doi_invalid", f"'{csl['DOI']}' is not a well-formed DOI"
            return
        try:
            reg = self.resolver.resolve_doi(doi)
        except NotFoundError as e:
            res.status, res.summary = "doi_invalid", str(e)
            res.checks.append({"check": "doi_registered", "ok": False, "message": str(e)})
            self._suggest_from_search(csl, res, replacing=doi)
            if "<" not in doi and re.search(r"\(\d{4,6}\)[\d:]*\d$", doi):  # '...(1997)025', Wiley's '...(199909)24:10'
                res.summary += (". It looks cut off before a '<...>' part (older Rumil versions did that to GSA/AGU "
                                "DOIs such as 10.1130/0091-7613(2001)029<0987:...>2.0.CO;2)")
            return
        self._check_registry(csl, reg, res, doi, acc)

    def verify_with(self, item: dict, reg: Resolution) -> VerifyResult:
        """Verification result for an item that was just created from registry record `reg` (no extra lookups)."""
        csl = item["csl"]
        res = VerifyResult(status="unverified", summary="")
        self._local_checks(item, res)
        if reg.authoritative and csl.get("DOI"):
            self._check_registry(csl, reg, res, normalize_doi(csl["DOI"]) or csl["DOI"], item.get("accepted"))
        elif reg.authoritative:
            res.registry = {"source": reg.source, "csl": reg.csl, "extras": reg.registry_record}
            self._compare(csl, reg, res, no_doi_status="verified_no_doi", accepted=item.get("accepted"))
        else:
            res.status = "url_ok" if csl.get("type") in WEB_TYPES else "unverified"
            res.summary = ("Saved from a web page that has no registry record; the link works"
                           if res.status == "url_ok" else
                           "Saved from web-page metadata only; no DOI or registry record confirms it")
        _suffix_repair(csl, res)
        self._duplicate_check(item, res)
        _note_pending_fixes(res)
        _number_suggestions(res)
        return res

    def _check_registry(self, csl: dict, reg: Resolution, res: VerifyResult, doi: str, acc: dict | None = None) -> None:
        res.checks.append({"check": "doi_registered", "ok": True, "message": f"Found in {reg.source}"})
        res.registry = {"source": reg.source, "csl": reg.csl, "extras": reg.registry_record}
        before = len(res.suggestions)
        self._compare(csl, reg, res, accepted=acc)
        extras = reg.registry_record or {}
        targets = extras.get("notice_for") or []
        if targets or (is_notice_title(reg.csl.get("title")) and not is_notice_title(csl.get("title"))):
            # The DOI is an erratum's or a retraction notice's, not the article's: never copy its details over.
            del res.suggestions[before:]
            res.status = "mismatch"
            res.summary = ("This DOI belongs to a correction or retraction notice"
                           + (f" for {targets[0]}" if targets else "") + ", not to the article itself")
            for t in targets[:1]:
                res.suggestions.append({"kind": "replace_doi", "field": "DOI", "current": doi, "proposed": t,
                                        "safe": False, "reason": f"Cite the article ({t}), not the notice that corrects it"})
            return
        if extras.get("retracted"):
            notices = [u for u in extras.get("updates", []) if (u.get("type") or "").lower() in ("retraction", "withdrawal", "removal")]
            when = notices[0]["date"] if notices else ""
            res.status = "retracted"
            res.summary = f"RETRACTED{' on ' + when if when else ''}" + (f" (notice {notices[0]['notice_doi']})" if notices and notices[0].get('notice_doi') else "")
        for u in extras.get("updates", []):
            if (u.get("type") or "").lower() in ("correction", "erratum", "corrigendum", "expression_of_concern", "addendum"):
                res.issues.append(f"{u.get('label') or u.get('type')} issued {u.get('date', '')}: https://doi.org/{u.get('notice_doi')}")
        for pdoi in extras.get("published_version_dois") or []:
            if doi_key(pdoi) != doi_key(doi):
                res.suggestions.append({"kind": "published_version", "proposed": pdoi, "current": doi, "safe": False,
                                        "reason": "This is a preprint; a peer-reviewed version has been published"})
        if reg.source == "crossref" and csl.get("type") not in ("article",) and extras.get("crossref_type") == "posted-content":
            res.issues.append("The DOI belongs to a preprint record")
        # arXiv DOIs (DataCite) don't carry journal links; ask arXiv itself.
        if doi.lower().startswith("10.48550/arxiv."):
            try:
                ar = self.resolver.arxiv(normalize_arxiv(doi) or doi)
                for pdoi in (ar.registry_record.get("published_version_dois") if ar else []) or []:
                    res.suggestions.append({"kind": "published_version", "proposed": pdoi, "current": doi, "safe": False,
                                            "reason": f"arXiv lists a published version ({ar.registry_record.get('journal_ref') or pdoi})"})
            except HttpError:
                pass

    def _verify_arxiv(self, csl: dict, res: VerifyResult, aid: str, acc: dict | None = None) -> None:
        reg = self.resolver.arxiv(aid)
        if not reg:
            res.status, res.summary = "unverified", f"arXiv has no record {aid}"
            return
        res.registry = {"source": "arxiv", "csl": reg.csl, "extras": reg.registry_record}
        self._compare(csl, reg, res, no_doi_status="verified_no_doi", accepted=acc)
        res.suggestions.append({"kind": "set_field", "field": "DOI", "current": None, "proposed": reg.csl["DOI"], "safe": True,
                                "reason": "arXiv DOI for this preprint"})
        for pdoi in reg.registry_record.get("published_version_dois") or []:
            res.suggestions.append({"kind": "published_version", "proposed": pdoi, "current": aid, "safe": False,
                                    "reason": f"arXiv lists a published version ({reg.registry_record.get('journal_ref') or pdoi})"})

    def _verify_isbn(self, csl: dict, res: VerifyResult, acc: dict | None = None) -> None:
        reg = self.resolver.isbn(csl["ISBN"])
        if not reg:
            self._verify_by_search(csl, res)
            return
        res.registry = {"source": reg.source, "csl": reg.csl, "extras": {}}
        self._compare(csl, reg, res, no_doi_status="verified_no_doi", accepted=acc)

    def _verify_url(self, csl: dict, res: VerifyResult) -> None:
        url = csl.get("URL")
        if not url or is_junk_url(url):
            if csl.get("type") in ("dataset", "software") and csl.get("title"):
                self._verify_by_search(csl, res)
                return
            res.status, res.summary = "unverified", "No DOI or working link to check"
            return
        try:
            resp = self.resolver.http.request(url, retries=1, timeout=20)
            ok = resp.status < 400 or resp.status in (401, 403, 405, 429)  # exists but blocks robots
        except HttpError as e:
            ok, resp = False, None
            res.checks.append({"check": "url", "ok": False, "message": str(e)})
        if ok:
            res.status, res.summary = "url_ok", f"Link responds (HTTP {resp.status})"
        else:
            res.status = "url_dead"
            res.summary = f"Link does not respond{' (HTTP ' + str(resp.status) + ')' if resp else ''}: {url}"

    def _verify_by_search(self, csl: dict, res: VerifyResult) -> None:
        if not csl.get("title"):
            res.status, res.summary = "unverified", "No title to search for"
            return
        if self._verify_lpsc(csl, res):
            return
        found = self._suggest_from_search(csl, res)
        if found == "doi":
            res.status = "doi_found"
            res.summary = "No DOI recorded; a matching registry record was found (see suggestions)"
        elif found == "nodoi":
            res.status = "verified_no_doi"
            res.summary = "Found in NASA ADS (no DOI)"
        else:
            res.status = "unverified"
            hint = "" if self.resolver.ads_token else " An ADS token (rumil config set ads_token ...) would let Rumil check conference abstracts."
            res.summary = "Could not find this work in Crossref" + (" or ADS" if self.resolver.ads_token else "") + "." + hint

    def _verify_lpsc(self, csl: dict, res: VerifyResult) -> bool:
        """LPSC abstracts have no DOIs; check the abstract PDF on the LPI server (works without an ADS token)."""
        info = lpsc_info(csl)
        if not info:
            return False
        year, num = info
        got = self.resolver.lpsc_abstract(year, num)
        if not got:
            res.issues.append(f"No LPSC {year} abstract #{num} on the LPI server")
            return False
        url, text = got
        fam = family_of(creators(csl)[0]) if creators(csl) else ""
        if title_in_text(csl.get("title"), text) and (not fam or fold(fam) in fold(text)):
            res.status = "verified_no_doi"
            res.summary = f"Found on the LPI abstract server: LPSC {year} abstract #{num}"
            res.checks.append({"check": "lpsc_abstract", "ok": True, "message": url})
            if not csl.get("URL"):
                res.suggestions.append({"kind": "set_field", "field": "URL", "current": None, "proposed": url, "safe": True,
                                        "reason": "Link to the abstract on the LPI server"})
            return True
        res.issues.append(f"LPSC {year} abstract #{num} ({url}) has a different title or authors; check the abstract number")
        return False

    def _suggest_from_search(self, csl: dict, res: VerifyResult, replacing: str | None = None) -> str | None:
        fam = family_of(creators(csl)[0]) if creators(csl) else ""
        cands = self.resolver.search(title=strip_markup(csl.get("title", "")), author=fam, year=year_of(csl), rows=5)
        errors = [c["error"] for c in cands if "error" in c]
        if errors and not any("csl" in c and c.get("source") == "crossref" for c in cands):
            # A failed search (rate limit, outage) is not evidence the work doesn't exist: report an error to retry.
            raise HttpError("; ".join(errors))
        best = next((c for c in cands if c.get("confidence") == "high"), None)
        if not best:
            med = next((c for c in cands if c.get("confidence") == "medium"), None)
            if med and med["csl"].get("DOI"):
                res.issues.append(f"Possible match (needs review): {med['csl'].get('title')} — https://doi.org/{med['csl']['DOI']}")
            return None
        bc = best["csl"]
        if bc.get("DOI"):
            # Fill only bibliographic details that are missing; never take authors or dates from a search hit.
            fill = {k: bc[k] for k in SEARCH_FILLABLE if k in bc and k not in csl}
            exact = (best["match"]["title_similarity"] >= 0.97 and best["match"]["author_match"] is True
                     and best["match"]["year_diff"] == 0 and _details_agree(csl, bc))
            # A DOI cut off at '<' by older Rumil versions ('10.1130/0091-7613(1997)025') is completed safely by the
            # registry DOI that starts with it.
            completes = bool(replacing) and bc["DOI"].lower().startswith(replacing.lower() + "<")
            res.suggestions.append({
                "kind": "replace_doi" if replacing else "add_doi", "field": "DOI", "current": replacing,
                "proposed": bc["DOI"], "fill": fill, "safe": bool(exact and (not replacing or completes)),
                "reason": f"{best['source']} match: '{bc.get('title')}' ({year_of(bc) or 'n.d.'}), "
                          f"title similarity {best['match']['title_similarity']}",
            })
            res.registry = {"source": best["source"], "csl": bc, "extras": {}}
            return "doi"
        if best["source"] == "ads":
            res.registry = {"source": "ads", "csl": bc, "extras": {}}
            for k in ("page", "container-title", "URL"):
                if bc.get(k) and not csl.get(k):
                    res.suggestions.append({"kind": "set_field", "field": k, "current": None, "proposed": bc[k], "safe": True,
                                            "reason": "Filled from the NASA ADS record"})
            return "nodoi"
        return None

    # -------------------------------------------------------------- compare
    def _compare(self, csl: dict, reg: Resolution, res: VerifyResult, no_doi_status: str = "verified",
                 accepted: dict | None = None) -> None:
        """Compare the library record with the registry record.

        mismatch: probably a different work (title or year disagree), so nothing is copied over.
        differs:  the same work, but a field that will be printed in the reference (title wording, author
                  names, volume, issue, pages) differs; a correction to the registry value is offered.
        """
        r = reg.csl
        acc = accepted or {}
        catalog = reg.source in CATALOG_SOURCES
        src = {"crossref": "Crossref", "datacite": "DataCite", "arxiv": "arXiv", "ads": "NASA ADS"}.get(reg.source, reg.source)
        problems: list[str] = []
        diffs: list[tuple[str, str, str, str]] = []  # (field, library text, registry text, issue text)
        first_new = len(res.suggestions)

        def correction(field_, proposed, reason):
            res.suggestions.append({"kind": "set_field", "field": field_, "current": csl.get(field_), "proposed": proposed,
                                    "safe": False, "correction": True, "reason": reason})

        def junk(field_, proposed, issue, reason):
            """The library value is a placeholder or carries export debris; the clean value is certain, so it's safe."""
            res.suggestions.append({"kind": "set_field", "field": field_, "current": csl.get(field_), "proposed": proposed,
                                    "safe": True, "reason": reason, "keepable": r.get(field_) is not None})
            diffs.append((field_, "", "", issue))

        # --- title: near-identical overall (same work), then word-for-word (what gets printed)
        reg_untitled = not strip_markup(r.get("title") or "").strip()
        if reg_untitled and csl.get("title"):
            # Some registry records carry no title at all. That says nothing about whether this is the same work,
            # so the title can't be checked; the DOI, year and authors still are.
            sim = 1.0
            res.checks.append({"check": "title", "ok": True, "message": f"the {src} record has no title to compare"})
            if "title" not in acc:
                res.issues.append(f"The {src} record has no title, so the library's title can't be checked against it "
                                  "(keep the library's title to stop this note)")
        else:
            sim = title_similarity(csl.get("title"), r.get("title"))
            res.checks.append({"check": "title", "ok": sim >= 0.85, "message": f"similarity {sim:.2f}"})
        if sim < 0.85:
            problems.append(f"title differs (registry: '{strip_markup(r.get('title'))[:120]}')")
        elif not catalog and r.get("title") and acc.get("title") != r.get("title"):
            self._compare_title(csl, r, res, diffs, src, correction, junk)

        # --- year
        ly, ry = year_of(csl), year_of(r)
        if ly and ry:
            diff = abs(ly - ry)
            res.checks.append({"check": "year", "ok": diff <= 1, "message": f"{ly} vs {ry}"})
            if diff > 1:
                problems.append(f"year differs (registry: {ry})")
            elif diff == 1:
                res.issues.append(f"Year {ly} vs registry {ry} (online-first vs print year is common)")

        # --- authors (people only: group names such as 'MERTIS Co-I Team' are often missing on one side)
        la = [_as_person(_split_suffix(n)) for n in creators(csl)]
        ra = [_as_person(n) for n in creators(r)]
        # A kept author list is stored as the registry gave it; compare it in the same normalized form as `ra`.
        # Comparing the raw stored list with `ra` never matched when the registry list held an organisation
        # ('literal') name, so a kept author list was flagged again on every verify.
        kept = acc.get("author")
        authors_kept = kept is not None and [_as_person(n) for n in creators({"author": kept})] == ra
        real_la = [n for n in la if not is_etal_name(n)]
        if la and ra:
            first_ok = family_matches(family_of(la[0]), family_of(ra[0]))
            res.checks.append({"check": "first_author", "ok": first_ok, "message": f"{family_of(la[0])} vs {family_of(ra[0])}"})
            same_work = sim >= 0.95 and not problems
            lp, rp = _persons(real_la) or real_la, _persons(ra) or ra
            truncated = first_ok and ((len(rp) > len(lp) and all(family_matches(family_of(a), family_of(b))
                                                                  for a, b in zip(lp, rp))) or len(real_la) < len(la))
            swapped = sum(_names_swapped(a, b) for a, b in zip(lp, rp))
            first_only = len(rp) == 1 and len(lp) > 1 and family_matches(family_of(lp[0]), family_of(rp[0]))
            if not first_ok and not swapped and not catalog and not same_work and not authors_kept and not any(
                    family_matches(family_of(la[0]), family_of(b)) for b in ra):
                problems.append(f"first author differs (registry: {family_of(ra[0])})")
            elif truncated and not swapped:
                res.suggestions.append({"kind": "set_field", "field": "author", "current": la, "proposed": ra, "safe": True,
                                        "reason": f"Registry lists {len(ra)} authors; library has {len(real_la)}"})
            elif first_only:
                res.issues.append(f"The {src} record lists only the first author; the library has {len(lp)}")
            elif not catalog and not authors_kept and (
                    swapped or len(lp) != len(rp) or any(not _same_family(a, b) for a, b in zip(lp, rp))):
                i = next((j for j, (a, b) in enumerate(zip(lp, rp))
                          if _names_swapped(a, b) or not _same_family(a, b)), min(len(lp), len(rp)))
                diffs.append(("author", _name_window(lp, i), _name_window(rp, i), ""))
                why = ("Given and family names are swapped" if swapped
                       else f"Author list differs from the {src} record ({len(lp)} vs {len(rp)} names)")
                correction("author", ra, why)
            elif not catalog and len(lp) == len(rp):
                merged, accents, expanded = _name_improvements(real_la, lp, rp)
                if accents and not authors_kept:
                    shown = "; ".join(f"{a} -> {b}" for a, b in accents[:3]) + (" ..." if len(accents) > 3 else "")
                    junk("author", merged, f"Authors: library names have lost or garbled accents that the {src} record has ({shown})",
                         f"Restore accents from the {src} record")
                elif expanded:
                    res.suggestions.append({"kind": "set_field", "field": "author", "current": la, "proposed": merged,
                                            "safe": True, "reason": f"Full given names from the {src} record "
                                                                    f"({expanded} name{'s' if expanded > 1 else ''})"})
        elif ra and not la:
            res.suggestions.append({"kind": "set_field", "field": "author", "current": None, "proposed": ra, "safe": True,
                                    "reason": "Authors missing; filled from registry"})

        # --- empty fields the registry can fill (safe)
        for k in FILLABLE:
            if k in ("author",) or (k in ("publisher", "publisher-place") and csl.get("type") == "article-journal"):
                continue
            if r.get(k) and not csl.get(k):
                res.suggestions.append({"kind": "set_field", "field": k, "current": None, "proposed": r[k], "safe": True,
                                        "reason": f"Missing; filled from {src}"})

        # --- AGU manuscript numbers (2006JE002868) are not citation numbers (E10007); NASA ADS has the real one.
        self._agu_numbers_and_abstract(csl, r, res, diffs, junk)

        # --- volume / issue / pages: printed in the reference, so they must agree
        for k in COMPARED:
            lraw, rraw = csl.get(k), r.get(k)
            label = "Pages" if k == "page" else k.capitalize()
            if not lraw or (rraw and acc.get(k) == rraw):
                continue
            if not rraw:
                if _is_junk_num(k, _canon_num(lraw)):
                    junk(k, None, f"{label}: library '{lraw}' is a placeholder", "Remove the placeholder")
                continue
            kind, val = _compare_number(k, lraw, rraw, csl.get("volume") or r.get("volume"))
            if kind == "fill":
                res.suggestions.append({"kind": "set_field", "field": k, "current": lraw, "proposed": val, "safe": True,
                                        "reason": f"Complete {k} from the {src} record"})
            elif kind == "junk":
                if val is None:
                    junk(k, None, f"{label}: library '{lraw}' is a placeholder", "Remove the placeholder")
                else:
                    junk(k, val, f"{label}: library '{lraw}' is a placeholder or export debris; {src} has '{rraw}'"
                         + (f" (use '{val}')" if val != rraw else ""),
                         f"Use the {src} value" if val == rraw else "Remove the export debris")
            elif kind == "note":
                res.issues.append(f"{label}: library '{lraw}'; {src} gives the AGU manuscript number '{rraw}', "
                                  "which can't confirm the citation number")
            elif kind == "diff":
                diffs.append((k, str(lraw), str(rraw), ""))
                correction(k, rraw, f"{k.capitalize()} differs from the {src} record")

        # --- journal name (abbreviations are fine; a different journal is not)
        lc, rc = fold(csl.get("container-title")), fold(r.get("container-title"))
        if lc and rc and not lc.startswith("\\") and title_similarity(lc, rc) < 0.6 and _abbrev_mismatch(lc, rc):
            res.suggestions.append({"kind": "set_field", "field": "container-title", "current": csl.get("container-title"),
                                    "proposed": r.get("container-title"), "safe": False,
                                    "reason": f"Journal/container name differs from {src}"})

        if problems:
            # The registry record may be a different work: never offer its values as fills.
            del res.suggestions[first_new:]
            res.status = "mismatch"
            res.summary = "Registry record disagrees: " + "; ".join(problems)
            res.suggestions.append({"kind": "replace_metadata", "safe": False,
                                    "proposed": {k: r[k] for k in ("title", "author", "issued", "container-title", "volume",
                                                                    "issue", "page", "type") if k in r},
                                    "reason": "Replace the core fields with the registry record (check the DOI is the work you meant first)"})
        elif diffs:
            res.status = "differs"
            fields = list(dict.fromkeys(d[0] if d[0] != "author" else "author names" for d in diffs))
            res.summary = (f"Same work as the {src} record, but the library's " + ", ".join(fields)
                           + (" differs" if len(fields) == 1 and fields[0] != "author names" else " differ")
                           + "; a reference built from the library would be wrong")
            for f, lv, rv, text in reversed(diffs):
                label = {"author": "Authors", "page": "Pages"}.get(f, f.capitalize())
                res.issues.insert(0, text or f"{label}: library '{lv[:160]}' vs {src} '{rv[:160]}'")
        else:
            res.status = no_doi_status if not csl.get("DOI") else "verified"
            res.summary = f"Matches {src} record" + (f" for DOI {csl['DOI']}" if csl.get("DOI") else "")

    def _agu_numbers_and_abstract(self, csl: dict, r: dict, res: "VerifyResult", diffs: list, junk) -> None:
        doi = normalize_doi(csl.get("DOI") or "") or ""
        page = str(csl.get("page") or "").strip()
        manuscript = is_agu_manuscript_number(re.split(r"\s*[-\u2013,]\s*", page)[0]) if page else False
        want_page = manuscript or (not page and not r.get("page") and doi.lower().startswith("10.1029/"))
        want_abstract = not csl.get("abstract") and not r.get("abstract")
        ads = None
        if doi and (want_page or want_abstract) and self.resolver.ads_token:
            try:
                ads = self.resolver.ads_by_doi(doi)
            except HttpError:
                ads = None
        ads_page = str((ads or {}).get("page", [""])[0] if isinstance((ads or {}).get("page"), list)
                       else (ads or {}).get("page") or "").strip()
        if is_agu_manuscript_number(ads_page) or ads_page.lower() in ("", "n/a", "na"):
            ads_page = ""
        if manuscript:
            if ads_page:
                junk("page", ads_page, f"Pages: library '{page}' is AGU's manuscript number, not the citation number; "
                                       f"NASA ADS gives '{ads_page}'", "Use the citation number from NASA ADS")
            else:
                why = ("NASA ADS has no citation number for it either" if self.resolver.ads_token else
                       "set an ADS token so Rumil can look it up, or enter it from the paper")
                diffs.append(("page", page, "", f"Pages: library '{page}' is AGU's manuscript number, not the citation "
                                                f"number printed on the paper (such as E10007); {why}"))
                res.suggestions.append({"kind": "set_field", "field": "page", "current": page, "proposed": None,
                                        "safe": False, "correction": True, "keepable": False,
                                        "reason": "Remove the manuscript number (better no number than a wrong one)"})
        elif want_page and ads_page:
            res.suggestions.append({"kind": "set_field", "field": "page", "current": None, "proposed": ads_page,
                                    "safe": True, "reason": "Citation number from NASA ADS"})
        if want_abstract and ads and ads.get("abstract"):
            res.suggestions.append({"kind": "set_field", "field": "abstract", "current": None,
                                    "proposed": strip_markup(ads["abstract"]), "safe": True,
                                    "reason": "Abstract from NASA ADS (used to check what a citation claims)"})

    @staticmethod
    def _compare_title(csl: dict, r: dict, res: "VerifyResult", diffs: list, src: str, correction, junk) -> None:
        """Word-for-word title check. Export debris in the library title (a Gale '(Author abstract)' tag, the
        journal's running head) gets a safe cleanup; a running head on the registry side is ignored; a registry
        title that stops before the library's subtitle is only noted; any other wording difference is a 'differs'."""
        lt, rt = csl.get("title") or "", r.get("title") or ""
        W = title_words
        if W(lt) == W(rt):
            return
        rmain, rhead = split_running_head(rt)
        targets = [W(rt)] + ([W(rmain)] if rhead else [])
        l1 = strip_database_tags(lt)
        lmain, lhead = split_running_head(l1)
        if W(l1) in targets:
            if l1 == lt:  # only the registry title carries a running head
                return
            junk("title", l1, f"Title: library title ends with a database tag ('{strip_markup(lt[len(l1):]).strip(' .')}'); "
                              f"use '{strip_markup(l1)}'", "Remove the database tag from the title")
            return
        if lhead and W(lmain) in targets:
            junk("title", lmain, f"Title: library title ends with the journal's running head ('{strip_markup(lhead)}'); "
                                 f"use '{strip_markup(lmain)}'", "Remove the journal's running head from the title")
            return
        for m in re.finditer(r"\s*[:;.?!\u2014\u2013]\s+|\s+-\s+", l1):
            if m.start() > 10 and W(l1[:m.start()]) in targets:
                res.issues.append(f"The {src} title stops before the library's subtitle "
                                  f"('{strip_markup(l1[m.end():])[:100]}'); check the library title against the paper")
                return
        proposed = rmain if rhead else rt
        diffs.append(("title", strip_markup(lt), strip_markup(proposed), ""))
        if strip_markup(proposed).isupper():  # registry title in capitals: don't copy that over
            res.suggestions.append({"kind": "set_field", "field": "title", "current": lt, "proposed": proposed,
                                    "safe": False, "correction": False,
                                    "reason": f"Title wording differs from the {src} record (which is in capitals; "
                                              "edit the library title by hand)"})
        else:
            correction("title", proposed, f"Title wording differs from the {src} record")

    def _duplicate_check(self, item: dict, res: VerifyResult) -> None:
        try:
            dups = self.lib.find_duplicates_of(item["csl"], exclude_id=item["id"])
        except Exception:
            return
        for d, why, _ in dups:
            if d:
                res.issues.append(f"Possible duplicate of @{d['citekey']} ({why})")
                res.suggestions.append({"kind": "merge_duplicate", "proposed": d["citekey"], "safe": False,
                                        "reason": f"Merge @{d['citekey']} into this item ({why}); its key stays usable as an alias"})


def _names_swapped(local: dict, reg: dict) -> bool:
    """'Armando, Azua-Bustos' vs 'Azua-Bustos, Armando'; 'Bethany, L. Ehlmann' vs 'Ehlmann, Bethany L.'"""
    lf, lg = local.get("family") or "", local.get("given") or ""
    rf, rg = reg.get("family") or "", reg.get("given") or ""
    if not (lf and lg and rf and rg) or family_matches(lf, rf):
        return False
    return family_matches(lf, rg.split()[0]) and name_key(lg).endswith(name_key(rf))


def _short_name(n: dict) -> str:
    if n.get("literal"):
        return n["literal"]
    return ", ".join(x for x in (n.get("family"), n.get("given")) if x)


_MOJIBAKE = ("\u00c3", "\u00c2", "\ufffd", "\u00e2\u20ac")


def _restores_accents(lib: str, reg: str) -> bool:
    """'Payre' vs 'Payr\u00e9': same letters, but the library lost the diacritics (and the registry isn't garbled).
    Typographic hyphens and quotes ('Elkins\u2010Tanton') and capitals don't count."""
    reg = normalize_punct(reg)
    return bool(lib and reg) and lib != reg and lib.isascii() and not reg.isascii() and fold(lib) == fold(reg) \
        and not any(m in reg for m in _MOJIBAKE) and not (reg.isupper() and not lib.isupper())


def _initials(given: str) -> list[str]:
    return [w[0].upper() for w in re.split(r"[\s.\-]+", ascii_fold(given)) if w]


def _only_initials(given: str) -> bool:
    """'M. S.', 'J.-P.', 'M.S.' are initials; 'Michael S.' and 'Ana' are not."""
    toks = [t for t in re.split(r"[\s\-]+", ascii_fold(given or "").strip()) if t]
    return bool(toks) and all(re.fullmatch(r"[A-Za-z]\.?|(?:[A-Za-z]\.){2,}", t) for t in toks)


def _family_key(name: str) -> str:
    """Family name as printed, ignoring case, accents, punctuation, particles, and Jr./III."""
    toks = [t for t in fold(name).split() if not is_name_suffix(t)]
    return name_key(_PARTICLE_PREFIX.sub("", " ".join(toks)))


_PARTICLE_PREFIX = re.compile(r"^(?:(?:von|van|de|der|den|di|da|du|del|della|des|la|le|ten|ter|zu|dos|das|do)\s+)+")


def _same_family(a: dict, b: dict) -> bool:
    """Strict: 'Deanne Rogers' vs 'Rogers' or 'Viviano' vs 'Viviano-Beck' would print differently, so they differ
    here, although family_matches() treats them as the same person for deciding whether it is the same work."""
    if a.get("literal") or b.get("literal"):
        return family_matches(family_of(a), family_of(b))
    fa, fb = family_of(a), family_of(b)
    return name_key(fa) == name_key(fb) or _family_key(fa) == _family_key(fb)  # 'Le Feuvre' = 'LEFEUVRE


def _suffix_repair(csl: dict, res: "VerifyResult") -> None:
    """Names split around Jr./III. 'Iii, Rossman P. Irwin' or family 'Jr.' print wrongly: a safe repair that marks the
    reference as differing. 'Head III, James W.' prints acceptably, so moving the III into the suffix (where styles
    place it correctly) is a quiet safe tidy-up. Skipped when a registry correction of the author list is on offer."""
    for role in ("author", "editor"):
        names = csl.get(role) or []
        fixed = [_split_suffix(n) for n in names]
        if fixed == names or any(sg.get("field") == role for sg in res.suggestions):
            continue
        changed = [(a, b) for a, b in zip(names, fixed) if a != b]
        broken = [a for a, _ in changed if is_name_suffix(a.get("family")) or is_name_suffix(a.get("given"))]
        shown = "; ".join(f"'{_short_name(a)}' -> '{_short_name(b)}, {b.get('suffix', '')}'" for a, b in changed)[:220]
        res.suggestions.append({"kind": "set_field", "field": role, "current": names, "proposed": fixed, "safe": True,
                                "reason": f"Put Jr./Sr./III in the name's suffix ({shown})"})
        if broken:
            res.issues.append(f"{role.capitalize()}s: names are split wrongly around a suffix ({shown})")
            if res.status in ("verified", "verified_no_doi"):
                res.status = "differs"
                res.summary = "The registry confirms the work, but an author name is split wrongly around Jr. or III"


def _as_person(n: dict) -> dict:
    """DataCite often gives a person as one string ('Michael Phillips', as a literal or all in 'family');
    compare those as names. Organizations stay as they are."""
    whole = n.get("literal") or (n.get("family") if not n.get("given") else "") or ""
    toks = whole.split()
    if 2 <= len(toks) <= 4 and "," not in whole and toks[0][:1].isupper() and toks[0].lower() not in _PARTICLE_WORDS \
            and not _GROUP_RE.search(whole) and not _ORG_WORDS.search(whole):
        return parse_name(whole)
    return n


_PARTICLE_WORDS = {"von", "van", "de", "der", "den", "di", "da", "du", "del", "della", "des", "la", "le", "ten", "ter"}


def _note_pending_fixes(res: "VerifyResult") -> None:
    """A verified reference can still print incompletely (pages '525' where the paper has 525-528). Say so."""
    safe = [sg for sg in res.suggestions if sg.get("safe") and sg.get("kind") == "set_field"
            and sg.get("field") in ("page", "volume", "issue", "author", "container-title", "DOI", "title")]
    if safe and res.status in ("verified", "verified_no_doi"):
        what = ", ".join(dict.fromkeys(sg["field"] if sg["field"] != "page" else "pages" for sg in safe))
        res.summary += f"; safe fixes available for {what} (apply_fixes applies them)"


def _well_formed_given(given: str) -> bool:
    """Capitalized words and initials only ('Joseph R.', 'Jean-Pierre'), not oddities like 'B. deB.'."""
    toks = [t for t in re.split(r"[\s\-]+", given.strip()) if t]
    return bool(toks) and all(t[0].isupper() and (re.fullmatch(r"\w\.?|(?:\w\.){2,}", t) or t.rstrip(".")[1:].islower())
                              for t in toks)


def _name_improvements(real_la: list[dict], lp: list[dict], rp: list[dict]) -> tuple[list[dict], list, int]:
    """Where the library and registry name the same people in the same order, take the registry's accents and full
    given names ('Payre, W.' -> 'Payr\u00e9, William'). Returns (merged list, accent changes, given names expanded)."""
    if any(_names_swapped(a, b) or not family_matches(family_of(a), family_of(b)) for a, b in zip(lp, rp)):
        return real_la, [], 0
    better = {}
    accents, expanded = [], 0
    for a, b in zip(lp, rp):
        new = dict(a)
        for f in ("family", "given"):
            lv, rv = a.get(f) or "", b.get(f) or ""
            damaged = bool(re.search(r"[\\{}]", lv)) and rv and not re.search(r"[\\{}]", rv) \
                and _initials(lv.replace("\\", ""))[:1] == _initials(rv)[:1]
            if _restores_accents(lv, rv) or damaged:  # damaged: LaTeX left in the name ('Val\\\u0155ie')
                new[f] = normalize_punct(rv)
                accents.append((lv, new[f]))
        lg, rg = a.get("given") or "", re.sub(r"\.(?=[A-Z])", ". ", normalize_punct(b.get("given") or ""))
        if _only_initials(lg) and rg and not _only_initials(rg) and _initials(lg) == _initials(rg) \
                and _well_formed_given(rg) and not any(m in rg for m in _MOJIBAKE):
            new["given"] = rg  # 'David M.H.' arrives as 'David M. H.'
            expanded += 1
        if new != a:
            better[id(a)] = new
    return [better.get(id(n), n) for n in real_la], accents, expanded


def _name_window(names: list[dict], i: int, width: int = 4) -> str:
    """A few names around position i (where two author lists first disagree)."""
    start = max(0, min(i, len(names)) - 1)
    seg = names[start:start + width]
    return (f"[{start + 1}] ... " if start else "") + "; ".join(_short_name(n) for n in seg) \
        + (" ..." if start + width < len(names) else "")


_GROUP_RE = re.compile(r"\b(team|teams|scientists|party|consortium|collaboration|group|members|investigators|project|"
                       r"committee|council|association|society|working|network|programme|program)\b", re.I)


def _is_group(n: dict) -> bool:
    if n.get("literal"):
        return True
    fam = n.get("family") or ""
    m = _GROUP_RE.search(fam)
    # 'Team, The SuperCam' (a group name split as if it were a person) or a family-only group name
    return bool(m) and (not n.get("given") or m.group(0) == fam.strip())


def _persons(names: list[dict]) -> list[dict]:
    """People only: drop group authors ('MERTIS Co-I Team', 'Third-Party Scientists', 'Team, The SuperCam')."""
    return [n for n in names if not _is_group(n)]


_DASHES = str.maketrans({"\u2013": "-", "\u2014": "-", "\u2010": "-", "\u2011": "-", "\u2212": "-"})
_JUNK_NUMS = {"0", "00", "000", "n/a", "na", "n.a.", "n/a-n/a", "na-na", "-", "?", "none", "null", "unknown", "nan",
              "x", "xx", "tbd"}


def _canon_num(v) -> str:
    s = str(v).translate(_DASHES).strip().lower()
    return re.sub(r"\s+", " ", re.sub(r"\s*-+\s*", "-", s))


def _is_junk_num(k: str, s: str) -> bool:
    """Placeholders exported in volume/issue/page fields: 'n/a', 'n/a-n/a', '0', or an issue of 'Volume 35, 2007'."""
    return s in _JUNK_NUMS or (k == "issue" and bool(re.search(r"\bvol", s))) \
        or (k == "page" and all(p.strip() in _JUNK_NUMS for p in s.split("-")))


def _split_range(s: str) -> tuple[str, str | None]:
    if "-" in s:
        a, b = s.split("-", 1)
        return a.strip(), b.strip()
    return s, None


def _page_key(s: str) -> str:
    s = re.sub(r"\s+", "", s)
    return s.lstrip("e").lstrip("0") or s


def _vi_key(s: str) -> str:
    """Comparable volume/issue: '01' = '1', 'E8' = '8', 'S1' = '1_suppl', 'XLII-3-W1' = 'XLII-3/W1'."""
    if "supp" in s or re.fullmatch(r"s\d+", s):
        return "s" + "".join(re.findall(r"\d", s))
    s = re.sub(r"[^a-z0-9]", "", s)
    if re.fullmatch(r"[a-rt-z]\d+", s):
        s = s[1:]
    return (s.lstrip("0") or "0") if s.isdigit() else s


def _compare_number(k: str, lraw, rraw, volume=None) -> tuple[str, Any]:
    """Compare a library volume/issue/page with the registry's.

    Returns ('same', None), ('fill', value) to complete it (issue '1' -> '1-4', pages '1093' -> '1093-1112'),
    ('junk', value) when the library holds a placeholder ('n/a', '0', 'E10007-n/a'), ('note', None) when the
    registry gives an AGU manuscript number instead of the citation number, or ('diff', None)."""
    L, R = _canon_num(lraw), _canon_num(rraw)
    if not L or not R or L == R:
        return "same", None
    if _is_junk_num(k, L):
        return "junk", (None if k == "page" and is_agu_manuscript_number(rraw) else rraw)
    ls, le = _split_range(L)
    rs, re_ = _split_range(R)
    if k != "page":
        if le is None and re_ is not None and _vi_key(ls) == _vi_key(rs):
            return "fill", rraw
        if _vi_key(L) == _vi_key(R) or (re_ is None and le is not None and _vi_key(ls) == _vi_key(rs)):
            return "same", None
        return "diff", None
    ls, rs = re.split(r"\s*[,;]\s*", ls)[0], re.split(r"\s*[,;]\s*", rs)[0]  # '111,IN1,119-...' (plates)
    raw = str(lraw).translate(_DASHES).strip()
    orig_start = re.split(r"\s*[-,;]+\s*", raw, maxsplit=1)[0].strip()
    cleaned = False
    if le is not None and (le in _JUNK_NUMS or not re.search(r"\d", le)):  # 'E10007-n/a', '5717-&', '344-+'
        cleaned, le = True, None
    if _page_key(ls) == _page_key(rs):
        if le is not None and re_ is not None and _page_key(le) == _page_key(re_):  # '960-968' vs '960-0968'
            return "same", None
        if re_ and (le is None or (len(le) < len(re_) and re_.endswith(le))):  # missing or abbreviated last page
            return ("junk" if cleaned else "fill"), rraw
        if cleaned:
            return "junk", orig_start
        if le is not None and re_ is None and _page_key(le) == _page_key(ls):  # 'eaap8306-eaap8306'
            return "fill", rraw
        return "same", None
    if is_agu_manuscript_number(str(rraw).strip()):
        return ("junk", orig_start) if cleaned else ("note", None)
    # SPIE-style article numbers start with the volume (vol. 11128 -> 111280T); Crossref often gives something else.
    vol = re.sub(r"\s+", "", _canon_num(volume)) if volume else ""
    if vol.isdigit() and len(vol) >= 3 and any(re.fullmatch(re.escape(vol) + r"[0-9a-z]{2}", x) for x in (ls, rs)) \
            and not (ls.startswith(vol) and rs.startswith(vol)):
        return ("junk", orig_start) if cleaned else ("same", None)
    # An article number on one side and the article's own pagination (1-12) on the other.
    if (re_ is None and ls == "1" and le) or (le is None and rs == "1" and re_):
        return ("junk", orig_start) if cleaned else ("same", None)
    return "diff", None


def _details_agree(local: dict, reg: dict) -> bool:
    """Volume, first page, and journal (where both sides have them) must agree for a search match to be safe."""
    for k in COMPARED:
        if local.get(k) and reg.get(k) and _compare_number(k, local[k], reg[k], local.get("volume"))[0] == "diff":
            return False
    lc, rc = fold(local.get("container-title")), fold(reg.get("container-title"))
    if lc and rc and title_similarity(lc, rc) < 0.6 and _abbrev_mismatch(lc, rc):
        return False
    return True


def _abbrev_mismatch(a: str, b: str) -> bool:
    """True unless one container name looks like an abbreviation of the other."""
    wa = [w for w in re.split(r"[^a-z]+", a) if w]
    wb = [w for w in re.split(r"[^a-z]+", b) if w]
    short, long_ = (wa, wb) if len(" ".join(wa)) <= len(" ".join(wb)) else (wb, wa)
    if not short:
        return False
    i = 0
    for w in long_:
        if i < len(short) and w.startswith(short[i]):
            i += 1
    return i < len(short)


def _arxiv_from(csl: dict) -> str | None:
    from .db import _arxiv_of
    return _arxiv_of(csl)


def _number_suggestions(res: VerifyResult) -> None:
    for i, s in enumerate(res.suggestions, 1):
        s["id"] = f"s{i}"
    order = {k: i for i, k in enumerate(STATUS_RANK)}
    res.status = res.status if res.status in order else "unverified"


def describe_status(status: str) -> str:
    return {
        "verified": "Verified against the DOI registry",
        "verified_no_doi": "Verified (no DOI) against arXiv/ISBN/ADS",
        "url_ok": "Link works (web source)",
        "mismatch": "Registry record looks like a different work",
        "differs": "Same work, but title/authors/pages differ from the registry",
        "doi_found": "Missing DOI found; review suggestion",
        "unverified": "Could not be verified",
        "url_dead": "Link is dead",
        "retracted": "Retracted",
        "doi_invalid": "DOI does not exist",
        "error": "Check failed (network)",
        "unchecked": "Not yet checked",
    }.get(status, status)
