"""MCP server that gives Claude direct access to the Rumil library.

Run with `rumil mcp` (stdio). Register it in Claude Desktop / Claude Code; see README.
"""

from __future__ import annotations

import os
import threading

try:  # MCP Python SDK v2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # v1
    from mcp.server.fastmcp import FastMCP as _Server

from . import __version__

INSTRUCTIONS = """Rumil is the user's citation library (imported from Zotero, verified against Crossref, DataCite, arXiv, and NASA ADS).

Rules for citing:
1. Never write a citation, DOI, author list, year, journal, volume, or page from memory. Every reference must come from Rumil.
2. To cite something: search_library first. If it isn't there, find_reference (searches registries), then add_reference with the DOI/arXiv id/ISBN/URL of the candidate you and the user agree on. Only then cite it.
3. If no registry has the work (e.g. an LPSC abstract, a NASA report), use add_reference_without_identifier with an evidence_url that shows the work; tell the user it is hand-entered.
4. Cite by citekey: in LaTeX \\citep{key}; in Markdown/Word drafts [@key]. Use format_citations to produce formatted text in a journal style, and format_word_document for .docx files.
5. Before a manuscript is final, run check_latex (for .tex) or verify_references on the cited keys and report anything retracted, mismatched, unverified, or missing.
6. When a claim is attributed to a paper, read_reference_fulltext (or the abstract) to confirm the paper says it; say so if you could not check.
7. Do not change reference metadata without telling the user; prefer apply_fixes with the 'safe' set."""

mcp = _Server("rumil", instructions=INSTRUCTIONS)

_svc = None
_svc_lock = threading.Lock()


def svc():
    global _svc
    with _svc_lock:
        if _svc is None:
            from .service import Rumil
            _svc = Rumil()
        return _svc


def _clip(items: list, n: int) -> list:
    return items[: max(1, min(int(n or 20), 200))]


# ----------------------------------------------------------------- reading
@mcp.tool()
def search_library(query: str = "", collection: str | None = None, tag: str | None = None, status: str | None = None,
                   year_from: int | None = None, year_to: int | None = None, limit: int = 20) -> dict:
    """Search the user's reference library. Use this before citing anything.

    query: words matched against citekeys, titles, authors, journals, abstracts, tags, notes, and DOIs (prefix match,
           all words must match). Field filters work too: author:smith title:"basaltic crust" year:2022 tag:mars doi:10.1130
    collection: collection name or "Parent/Child" path. status: verified | attention | problem | unchecked, or a
    specific status such as retracted, mismatch, doi_found, unverified.
    Returns citekeys with title, authors, year, container, DOI, and verification status."""
    s = svc()
    items, total = s.lib.search(query, collection=collection, tag=tag, status=status, year_from=year_from,
                                year_to=year_to, limit=max(1, min(limit, 200)))
    return {"total": total, "items": [s._item_out(i, brief=True) for i in items]}


@mcp.tool()
def get_reference(citekey: str, include_abstract: bool = True, include_notes: bool = False,
                  include_bibtex: bool = False) -> dict:
    """Full record for one reference: CSL-JSON metadata, verification result and suggested fixes, collections, tags,
    attachments (PDF paths), and optionally the user's notes and the BibTeX entry. Former citekeys (aliases) also work."""
    s = svc()
    out = s.item_detail(citekey, include_abstract=include_abstract, include_notes=include_notes)
    if include_bibtex:
        from .bibtex import bib_entry
        out["bibtex"] = bib_entry(out["citekey"], s.lib.require(citekey)["csl"], protected=s.cfg.get("protected_words") or [])
    return out


@mcp.tool()
def find_reference(query: str = "", title: str = "", author: str = "", year: int | None = None, limit: int = 5) -> dict:
    """Find a work to cite: searches the library and then Crossref (plus NASA ADS when configured).

    Give a title (best), and optionally first-author family name and year. Candidates are real registry records with a
    confidence rating; 'in_library_as' shows ones already saved. To use a candidate, call add_reference with its DOI.
    Never cite a candidate without adding it first."""
    return svc().find(query, title=title, author=author, year=year, limit=max(1, min(limit, 20)))


# ------------------------------------------------------------------ adding
@mcp.tool()
def add_reference(identifier: str, collection: str | None = None, tags: list[str] | None = None,
                  citekey: str | None = None) -> dict:
    """Add a reference by identifier; metadata is taken from the issuing registry, so it is accurate by construction.

    identifier: DOI (10.1016/j.icarus.2014.11.027 or a doi.org link), arXiv id or URL, ISBN, PMID, NASA ADS bibcode, or
    any article/web page URL (DOI and metadata are read from the page). Returns the new citekey and its verification
    status. If the work is already in the library, returns the existing entry instead of making a duplicate."""
    s = svc()
    return s.add(identifier, collection=collection, tags=tags or [], citekey=citekey, actor="claude")


@mcp.tool()
def add_reference_without_identifier(csl_json: dict, evidence_url: str, collection: str | None = None,
                                     tags: list[str] | None = None, citekey: str | None = None) -> dict:
    """Add a work that has no DOI/arXiv/ISBN (e.g. an LPSC abstract, a mission report, a thesis).

    csl_json: CSL-JSON fields, e.g. {"type": "paper-conference", "title": ..., "author": [{"family": ..., "given": ...}],
    "issued": {"date-parts": [[2022]]}, "container-title": "53rd Lunar and Planetary Science Conference",
    "page": "1234", "URL": ...}. evidence_url: a page or PDF that shows the work; Rumil checks the title and first author
    appear there. Only use metadata you read from the evidence, and tell the user this entry was hand-entered."""
    s = svc()
    return s.add_manual(csl_json, evidence_url=evidence_url, collection=collection, tags=tags or [], citekey=citekey,
                        actor="claude")


@mcp.tool()
def update_reference(citekey: str, fields: dict | None = None, new_citekey: str | None = None,
                     add_tags: list[str] | None = None, reason: str = "") -> dict:
    """Edit a reference's CSL-JSON fields (set a field to null to remove it), rename its citekey (the old key keeps
    working as an alias), or add tags. Only make changes the user asked for or approved; the edit is logged."""
    s = svc()
    it = s.lib.require(citekey)
    if fields:
        s.lib.update_item(it["id"], patch=fields, actor="claude", reason=reason or "edited by Claude")
    if new_citekey:
        s.lib.set_citekey(it["id"], new_citekey, actor="claude")
    if add_tags:
        s.lib.add_tags([it["id"]], add_tags)
    s.changed()
    return s.item_detail(it["id"], include_abstract=False, include_notes=False)


# ------------------------------------------------------------ verification
@mcp.tool()
def verify_references(citekeys: list[str] | None = None, collection: str | None = None, entire_library: bool = False,
                      only_unchecked: bool = False, apply_safe_fixes: bool = False) -> dict:
    """Check references against their registries (Crossref, DataCite, arXiv, ISBN services, ADS).

    Detects DOIs that don't exist, records that are a different work (mismatch), records that are the same work but
    whose title wording, author names, volume, issue, or pages differ from the registry (differs; a reference built
    from them would be wrong), retractions and corrections, preprints with a published version, missing DOIs (and
    finds them), dead links, and duplicates. apply_safe_fixes fills empty fields from the registry and removes junk
    without changing anything already set. More than 25 references run in the background: poll
    library_status(job_id=...)."""
    return svc().verify(citekeys, collection=collection, everything=entire_library, only_unchecked=only_unchecked,
                        apply_safe=apply_safe_fixes)


@mcp.tool()
def apply_fixes(citekey: str, suggestion_ids: list[str] | None = None, include_corrections: bool = False,
                keep_library_values: list[str] | None = None) -> dict:
    """Apply verification suggestions to one reference.

    Default: only the 'safe' ones (filling empty fields from the registry, removing junk).
    include_corrections=True also applies registry corrections for a reference whose status is 'differs' (same work,
    but the title wording, author names, volume, issue, or pages disagree with the registry): show the user the
    differences first. suggestion_ids applies specific ones (e.g. ["s2"] to cite a preprint's published version).
    keep_library_values=["title"] records that the library's value is intended, so verification stops flagging it."""
    s = svc()
    which = suggestion_ids or ("corrections" if include_corrections else ("safe" if not keep_library_values else None))
    return s.apply_fixes(citekey, which, keep=keep_library_values or [])


# ---------------------------------------------------------------- output
@mcp.tool()
def format_citations(citations: list[str], style: str | None = None, output: str = "text",
                     include_bibliography: bool = True) -> dict:
    """Format in-text citations and the matching reference list in a CSL style.

    citations: one string per citation, in document order, using pandoc syntax: "[@key]", "[@a; @b]",
    "[see @key, p. 12]", "[-@key]" (year only), "@key" (narrative: Phillips et al. (2023)). Order matters for numeric
    styles and for 2023a/2023b disambiguation, so pass every citation in the document in one call.
    style: a style id or alias (agu, jgr-planets, grl, icarus, psj, aas, gsa, pss, epsl, maps, nature, science, apa,
    lpsc, ieee, chicago) or any CSL repository id; defaults to the user's default style.
    output: "text", "html", or "runs" (text segments with italic/bold/superscript flags, for inserting into Word).
    Missing keys come back as [MISSING: key]; warnings flag unverified or retracted works."""
    s = svc()
    return s.formatter.cite_strings(citations, style, output=output, bibliography=include_bibliography)


@mcp.tool()
def format_bibliography(citekeys: list[str], style: str | None = None, output: str = "text") -> dict:
    """Formatted reference-list entries for the given citekeys, sorted as the style requires."""
    return svc().formatter.bibliography(citekeys, style, output=output)


@mcp.tool()
def export_references(format: str = "bibtex", citekeys: list[str] | None = None, collection: str | None = None,
                      path: str | None = None) -> dict:
    """Export references as bibtex, biblatex, csljson, or ris: specific citekeys, a collection, or (neither) the whole
    library. With path, writes the file there (a folder path gets references.bib/.json/.ris); otherwise returns text.
    Note: ~/Rumil/library.bib is always kept up to date automatically for LaTeX."""
    s = svc()
    out = s.export(format, citekeys, collection, path)
    if "text" in out and len(out["text"]) > 60000:
        out["text"] = out["text"][:60000]
        out["truncated"] = True
    return out


@mcp.tool()
def check_latex(tex_path: str, write_bib: str | None = None, biblatex: bool = False, overwrite: bool = False,
                import_missing: bool = False) -> dict:
    """Audit a LaTeX manuscript: follows \\input/\\include, finds every \\cite-family command, and reports keys that
    are missing from the library (with likely matches), unverified, retracted, mismatched, or differing from the
    registry (title wording, author names, pages). import_missing=True first imports cited entries that exist only in
    the document's own .bib (keeping their keys) and verifies them, so every citation is checked. With write_bib (e.g.
    "rumil-refs.bib", relative to the .tex), writes a .bib with exactly the cited references under the document's
    keys, with a RUMIL WARNING comment above any entry that isn't verified. manuscript_vs_library lists, entry by
    entry, where the document's own .bib and the library disagree; manuscript_better=True marks cases where the
    document's entry is more complete (accents, full given names, page ranges). Show those to the user before
    suggesting that the document switch to the Rumil-written .bib, and fix the library records first. fixable lists
    verified entries that would still print incompletely (a start page without the end page, initials where the
    registry has full names) until their safe fixes are applied with apply_fixes. An existing .bib that Rumil didn't write
    is never replaced unless overwrite=True (ask the user first). Paths are on the user's computer."""
    return svc().check_latex(tex_path, write_bib=write_bib, biblatex=biblatex, overwrite=overwrite,
                             import_missing=import_missing)


@mcp.tool()
def format_word_document(docx_path: str, style: str | None = None, output_path: str | None = None) -> dict:
    """Turn [@citekey] markers in a .docx into formatted citations and a reference list, saved as a NEW file
    (default: <name>-cited.docx next to the original). Put [@bibliography] on its own line where the references
    should go (otherwise they're appended under "References"). Unknown keys are left in place and highlighted yellow."""
    s = svc()
    from .docx_render import render_docx
    return render_docx(s.formatter, docx_path, style=style, out_path=output_path)


@mcp.tool()
def read_reference_fulltext(citekey: str, query: str | None = None, max_chars: int = 15000, offset: int = 0) -> dict:
    """Read the paper behind a reference (Zotero's full-text index or the attached PDF) to confirm what it says.
    With query, returns only passages containing those words. Otherwise returns text from offset (page through with
    offset). Falls back to the abstract when no attachment is readable."""
    from .fulltext import item_fulltext
    return item_fulltext(svc().lib, citekey, max_chars=max(500, min(max_chars, 60000)), offset=offset, query=query)


# ------------------------------------------------------------ organization
@mcp.tool()
def manage_collections(action: str = "list", name: str | None = None, new_name: str | None = None,
                       parent: str | None = None, citekeys: list[str] | None = None) -> dict:
    """Collections (folders) of references. action: list | create | rename | add_items | remove_items | delete.
    name accepts a "Parent/Child" path. delete removes the collection only, never the references in it."""
    s = svc()
    lib = s.lib
    if action == "list":
        return {"collections": lib.collections_tree()}
    if not name:
        raise ValueError("name is required")
    if action == "create":
        out = lib.create_collection(name, parent)
    elif action == "rename":
        out = lib.rename_collection(name, new_name or "")
    elif action == "add_items":
        out = {"added": lib.add_to_collection(name, citekeys or [])}
    elif action == "remove_items":
        out = {"removed": lib.remove_from_collection(name, citekeys or [])}
    elif action == "delete":
        lib.delete_collection(name)
        out = {"deleted": name}
    else:
        raise ValueError("action must be list, create, rename, add_items, remove_items, or delete")
    s.changed()
    return out


@mcp.tool()
def duplicates(action: str = "list", keep: str | None = None, remove: list[str] | None = None) -> dict:
    """List groups of references that look like the same work, or merge them (action="merge", keep=citekey,
    remove=[citekeys]). Merging keeps one record, combines tags/collections/notes/PDFs, and makes the removed citekeys
    aliases so documents citing them still work. Ask the user before merging."""
    s = svc()
    if action == "list":
        groups = s.lib.duplicate_groups()
        return {"groups": [[s._item_out(i, brief=True) for i in g] for g in groups[:100]], "count": len(groups)}
    if action == "merge":
        if not keep or not remove:
            raise ValueError("merge needs keep and remove")
        r = s.lib.merge(keep, remove, actor="claude")
        s.changed()
        return {"kept": r["citekey"], "merged": r.get("merged_citekeys"), "aliases": r.get("aliases")}
    raise ValueError("action must be list or merge")


@mcp.tool()
def sync_zotero() -> dict:
    """Import new and changed items from the user's local Zotero library (collections, tags, notes, PDFs). Safe to
    repeat; edits made in Rumil are never overwritten."""
    s = svc()
    from .zotero import import_zotero
    rep = import_zotero(s.lib)
    s.changed()
    for k in ("merged_identical",):
        rep[k] = rep[k][:20]
    return rep


@mcp.tool()
def import_bibliography_file(path: str, collection: str | None = None, verify: bool = True) -> dict:
    """Import a .bib, .ris, or CSL-JSON file (e.g. a paper's references.bib or a co-author's list). Keeps the file's
    citekeys; entries already in the library are linked (their file key becomes an alias) instead of duplicated.
    New entries are verified against the registries right away (more than 25 run in the background)."""
    return svc().import_bibfile(path, collection=collection, verify=verify)


@mcp.tool()
def citation_styles(install: str | None = None) -> dict:
    """List available CSL styles, or install one by its CSL repository id (e.g. "journal-of-geophysical-research-planets",
    "icarus", "the-astrophysical-journal"); browse ids at https://www.zotero.org/styles."""
    s = svc()
    if install:
        p = s.styles.install(install)
        return {"installed": p.stem, "path": str(p)}
    return {"default": s.cfg.get("default_style"), "styles": s.styles.list()}


@mcp.tool()
def library_status(job_id: str | None = None) -> dict:
    """Library summary (size, verification status counts, last Zotero sync, where library.bib lives, setup_warnings:
    missing settings that limit verification, and update_available when a newer Rumil release exists; pass both on to
    the user) or a background job's progress."""
    s = svc()
    if job_id:
        j = s.jobs.get(job_id)
        if not j:
            raise ValueError(f"No job {job_id} (jobs are kept only while this server runs)")
        return j.to_dict()
    st = s.lib.stats()
    st["auto_export"] = {k: str(s.cfg.resolve(v)) for k, v in (s.cfg.get("auto_export") or {}).items() if v}
    from .config import setup_warnings
    st["setup_warnings"] = setup_warnings(s.cfg)  # tell the user about these
    try:
        from .updater import check
        u = check(s.cfg, s.http, use_cache=True)  # asks GitHub at most once a day
        if u.get("update_available"):
            st["update_available"] = {"latest": u["latest"], "page": u.get("page"), "how": "rumil update"}
    except Exception:
        pass
    st["crossref_polite_pool"] = bool(s.cfg.get("contact_email"))
    st["ads_enabled"] = bool(s.cfg.get("ads_token"))
    st["default_style"] = s.cfg.get("default_style")
    st["jobs"] = s.jobs.all()[:5]
    st["version"] = __version__
    return st


def main() -> None:
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    try:
        mcp.run()
    finally:
        if _svc is not None:
            _svc.flush()


if __name__ == "__main__":
    main()
