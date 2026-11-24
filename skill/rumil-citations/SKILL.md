---
name: rumil-citations
description: Use whenever writing, citing, formatting, or checking references in a paper, proposal, abstract, LaTeX file, or Word document. Every citation must come from the user's Rumil library (the rumil MCP tools), never from memory.
---

# Citing with Rumil

Rumil is the user's citation library. It is imported from their Zotero library and checked against Crossref, DataCite, arXiv, ISBN services, PubMed, the LPI abstract server, and NASA ADS. Its tools appear as `rumil` tools (in Cowork they carry a longer prefix ending in `rumil__`).

## Rules

1. **Never write a citation from memory.** Author lists, years, journals, volumes, pages, and DOIs must come from Rumil records. This includes "well-known" papers.
2. **Look in the library first:** `search_library` (supports `author:`, `title:`, `year:`, `tag:` filters). Use the citekey it returns.
3. **If the work isn't in the library:** `find_reference` with the title (plus first author and year if known). Show the user the high-confidence candidate when there is any doubt about which work they mean, then `add_reference` with its DOI (or arXiv id, ISBN, URL). Cite only after it is added.
4. **No identifier exists** (LPSC/AGU abstract, NASA report, thesis): use `add_reference_without_identifier` with an `evidence_url` (the abstract PDF or landing page), filling fields only from that evidence, and tell the user it was hand-entered.
5. **Nothing found anywhere:** do not invent a reference. Write `[CITATION NEEDED: <what the claim needs>]` and list these for the user.
6. **Claims must match the source.** When a sentence attributes a specific result or number to a paper, check it with `read_reference_fulltext` (use `query=` for the relevant terms) or the abstract from `get_reference`. If you couldn't check, say so.
7. **Don't change reference metadata silently.** `apply_fixes` with no ids applies only the safe fixes (filling empty fields from the registry, completing page or issue ranges, removing export debris such as "(Author abstract)" tags, journal running heads, and "n/a" pages). Anything else (registry corrections, replacing a preprint with its published version, merging duplicates) needs the user's OK.
8. **Status "differs" means the printed reference would be wrong.** The registry confirms the work, but the library's title wording, author names, volume, issue, or pages disagree (for example "Scaling impact-melt..." where the paper says "Scaling impact melting..."). Never cite such an entry as-is. Show the user each difference (the item's `issues` list it as "Title: library '...' vs Crossref '...'"), then either `apply_fixes(key, include_corrections=True)` or, if the user says their version is deliberate, `apply_fixes(key, keep_library_values=["title"])`.
9. **Before a manuscript is final,** audit it: `check_latex` for .tex (with `import_missing=True` the first time, so references that are only in the paper's own .bib come under Rumil and get checked), or `verify_references` on every cited key. Report retracted, mismatched, differing, unverified, and missing references in a short list.
10. **Mention setup gaps once per session.** If `library_status` returns `setup_warnings` (no `contact_email`, so Crossref rate-limits Rumil; no `ads_token`, so conference abstracts can't be verified, older AGU papers can't get their citation numbers such as E10007, and many abstracts for claim checks are missing), tell the user and give them the fix (`rumil config set contact_email ...` / `rumil config set ads_token ...`). Checks that hit a rate limit end up as status "error"; rerun them with `verify_references(only_unchecked=True)`. If `library_status` reports `update_available`, tell the user once that a newer Rumil exists and that `rumil update` installs it.

## Citing in each format

**LaTeX.** Use `\citep{key}` / `\citet{key}` (natbib) or `\parencite` / `\textcite` (biblatex). Then run `check_latex(tex_path, write_bib="rumil-refs.bib")`. It finds every cite command (following `\input`/`\include`), reports problems, and writes a .bib with exactly the cited, registry-checked entries under the keys the document uses. Entries that still need attention carry a `% RUMIL WARNING` comment in that .bib; resolve them before submission. If the document already has its own .bib, the report's `manuscript_vs_library` lists entry by entry where the two disagree; where `manuscript_better` is true (accents, full given names, page ranges, citation numbers), update the library record before suggesting the document switch to the Rumil-written .bib. The report's `fixable` list holds verified entries that would still print incompletely (for example, a start page without the end page); offer to apply their safe fixes. It won't replace a .bib that Rumil didn't write; only pass `overwrite=True` if the user says so. The whole library is also always at `~/Rumil/library.bib`.

**Markdown, Docs, or plain drafts.** Write pandoc markers: `[@key]`, `[@a; @b]`, `[see @key, p. 12]`, `[-@key]` (year only), `@key` (narrative). To produce final text, call `format_citations` once with every citation in document order (order controls numbering and 2023a/2023b suffixes) and the target style; it returns each in-text citation plus the reference list.

**Word file on disk.** Put `[@key]` markers in the document and `[@bibliography]` on its own line where the references go, then call `format_word_document(docx_path, style)`. It writes `<name>-cited.docx` next to the original (the original is untouched) and highlights unknown keys in yellow.

**Word document open in the Word connector.** Draft with `[@key]` markers using the Word tools. When the text is settled, read the document text, collect every marker in order, call `format_citations(..., output="runs")`, replace each marker with its formatted text, and insert the reference list under the References heading. Apply italics where a run has `italic: true` (journal names, volumes in AGU style). If the document has been saved to disk, `format_word_document` on the saved file is the more reliable route.

## Styles

Use the journal's style: JGR Planets or GRL `agu`, Icarus `icarus`, PSS `pss`, EPSL `epsl`, PSJ `psj`, ApJ `aas`, Geology/GSA `gsa`, MAPS `maps`, Remote Sensing `remote-sensing`, Nature `nature`, Science `science`, LPSC abstracts `lpsc`, APA `apa`. Any other journal: `citation_styles(install="<CSL id>")` (ids at https://www.zotero.org/styles). Without a stated target, use Rumil's default style.

## Other jobs

- **Reference list someone else wrote** (a co-author's draft, a reviewer's list): for each entry, `search_library`; if absent, `find_reference`. Report entries that don't match any registry record, have wrong years/volumes/pages, or are retracted. Don't add them to the library unless the user asks.
- **Co-author .bib files, or a paper's own references.bib:** `import_bibliography_file` keeps their keys (works already in the library become aliases, so their .tex still compiles) and verifies the new entries; report the ones that come back differing, mismatched, or unverified.
- **Zotero:** `sync_zotero` pulls in anything the user saved to Zotero since the last sync.
- **Housekeeping:** `duplicates` lists likely duplicates; merge only with the user's OK. `library_status` shows verification counts and background-job progress.

## If the Rumil tools are missing

Say that the Rumil tools aren't connected in this session (the Claude desktop app or Claude Code must be running on the computer where Rumil is installed; `rumil doctor` checks the installation). Don't fall back to citing from memory. Mark citations `[CITATION NEEDED: ...]` instead.
