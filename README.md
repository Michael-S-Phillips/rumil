# Rumil

[![tests](https://github.com/michael-s-phillips/rumil/actions/workflows/tests.yml/badge.svg)](https://github.com/michael-s-phillips/rumil/actions/workflows/tests.yml) [![latest release](https://img.shields.io/github/v/release/michael-s-phillips/rumil)](https://github.com/michael-s-phillips/rumil/releases/latest)

A citation library that Claude can use directly, built to keep false or broken citations out of your papers. It imports your Zotero library, checks every reference against the registry that issued it, and produces citations for LaTeX and Word.

New users: start with [GETTING-STARTED.md](GETTING-STARTED.md), a one-page setup guide.

Rumil is named for the loremaster in Tolkien's legendarium who invented writing.

## What it does

**Your Zotero library, imported and kept in sync.** Rumil reads `~/Zotero/zotero.sqlite` directly (Zotero can stay open) and brings over items, creators, collections (including group libraries), tags, notes, PDF attachments, and Zotero citation keys. Running the import again picks up new and changed Zotero items. If you edit an item in Rumil, a later Zotero change never overwrites your edit. Zotero items that share a DOI are merged into one record, and the extra keys keep working as aliases.

**Verification against registries.** Each reference is checked against Crossref, DataCite, doi.org, arXiv, Open Library/Google Books, PubMed, and (with a free token) NASA ADS. Rumil reports:

| Status | Meaning |
|---|---|
| verified | The DOI is registered and its record agrees with yours: the full title word for word, the authors, year, volume, issue, and pages |
| verified (no DOI) | The work was confirmed through arXiv, an ISBN, ADS, or an evidence page |
| link works | Web page, software, or data set without a DOI whose link responds |
| differs from registry | Same work as the registry record, but something that gets printed in the reference (a word of the title, an author's name, the volume, issue, or pages) is different in your library |
| metadata mismatch | The DOI exists but points to a different title, author, or year |
| DOI found | No DOI was recorded, but a confident registry match exists |
| unverified | Nothing confirms the work exists as described |
| dead link | The only link does not respond |
| retracted | Crossref or Retraction Watch lists a retraction or withdrawal |
| DOI not found | The DOI is not registered anywhere |

Titles are compared word for word, so "Scaling impact-melt and crater dimensions" is caught when the paper says "Scaling impact melting and crater dimensions". Case, accents, punctuation, and hyphen-versus-space don't count as differences. Neither do quirks of the registry records themselves: the running head some publishers append to the title ("...on Mars: RECENT WIDE-SPREAD VOLCANISM ON MARS"), group authors such as "MERTIS Co-I Team", a record that lists only the first author, AGU manuscript numbers given in place of the citation number, or an article number on one side and the article's own page numbers (1-12) on the other.

Verification also flags corrections and errata (and never offers an erratum's or a retraction notice's DOI as the article's), preprints that now have a published version, names split wrongly around Jr. or III, truncated author lists ("et al." typed in as an author), placeholder URLs left by Web of Science exports, and likely duplicates. Fixes come in three kinds:

- **Safe fixes** fill empty fields from the registry, complete a range (pages "1093" to "1093-1112", issue "1" to "1-4"), expand initials to full given names and restore lost accents when the registry names the same people ("Payre, V." to "Payré, Valérie"), or remove export debris: tags such as "(Author abstract)" that Gale exports add to titles, a running head stuck to the title, and placeholder values such as pages "n/a" or volume "0". They never replace anything you typed.
- **Corrections** replace a printed field with the registry's value when the work is otherwise confirmed (status "differs from registry"). Apply them per item, or with `rumil verify --correct`.
- **Keep mine** records that your value is deliberate (for example, a title in a form the journal prefers), and Rumil stops flagging that field until the registry record changes.

Every edit is logged in the item's history.

**Claude integration (MCP server).** Claude gets tools to search the library, find a work in Crossref/ADS, add it by DOI/arXiv/ISBN/URL, verify references, format citations in any CSL style, export BibTeX, audit a LaTeX manuscript, format citations in a Word document, and read the paper itself (Zotero's full-text index or the PDF) to check that a cited claim is actually in it. The server tells Claude never to write a citation from memory.

**LaTeX.**
- `~/Rumil/library.bib` is rewritten whenever the library changes, so `\bibliography{~/Rumil/library}` always works.
- `rumil check-tex paper.tex --write-bib rumil-refs.bib` follows `\input`/`\include`, finds every `\cite`-family command (natbib and biblatex), reports keys that are missing (with likely matches), unverified, retracted, mismatched, or differing from the registry, and writes a `.bib` with exactly the cited entries under the keys your document uses. Entries that still need attention carry a `% RUMIL WARNING` comment in the written `.bib`. It refuses to replace a `.bib` it didn't write unless you add `--overwrite`.
- `--import-missing` brings cited entries that are only in the document's own `.bib` into the library (keeping their keys) and verifies them, so a paper written before Rumil comes fully under it. Entries that match a library item by title and first author become aliases of that item, even when the `.bib` types or dates them differently.
- The report also lists verified entries that would still print incompletely, such as a start page without the end page, until their safe fixes are applied.
- When the document has its own `.bib`, the report lists, entry by entry, where it and the library disagree, and marks the cases where the document's entry is more complete (accents, full given names, page ranges, citation numbers). Fix those in the library before switching the document to the `.bib` Rumil writes.
- BibTeX titles brace-protect acronyms (CRISM, HiRISE), formulas (SiO2), proper nouns in sentence-case titles, and a built-in list of planetary names, places, and missions, so AGU/AAS/Elsevier `.bst` files don't lowercase "Mars" or "Hellas".

**Word.** Write `[@citekey]` markers (pandoc syntax) in a draft, then run `rumil render-docx draft.docx --style icarus`. Rumil writes `draft-cited.docx` with formatted in-text citations and a reference list (with italics and hanging indents), leaving your original untouched. Claude can also insert formatted citations straight into an open Word document using the Word connector, with Rumil supplying the formatted text.

**Citation styles.** Every CSL style works: your Zotero styles are copied in at setup, and built-in styles include AGU (JGR Planets, GRL), Icarus, Planetary and Space Science, EPSL (Elsevier Harvard), PSJ/ApJ/AJ (AAS), GSA, Meteoritics & Planetary Science, MDPI/Remote Sensing, Nature, Science, APA, Chicago, IEEE, and a compact numbered style for LPSC and other LPI meeting abstracts (`lpsc`). Install any other style from the CSL repository with `rumil styles --install <id>`. Rumil handles dependent styles and 2023a/2023b year suffixes, plus narrative ("Phillips et al. (2023)") and year-only citations.

**Library window.** A Zotero-like three-pane app at http://127.0.0.1:23120 with collections, tags, search, a verification panel with one-click fixes, a citation preview in your chosen style, field editing, notes, PDFs, duplicate merging, import, and export.

**Browser button.** The "Save to Rumil" Chrome extension saves the paper you're reading. It reads the page's DOI or arXiv id and pulls metadata from the registry, then flags the item if it's already in your library. A page without a DOI is saved but marked unverified.

## Install (macOS)

Download `rumil-vX.Y.Z.zip` from the latest release at https://github.com/michael-s-phillips/rumil/releases/latest and unzip it (double-click it in Finder). Then, in Terminal:

```bash
cd ~/Downloads/rumil      # wherever you unzipped it
bash install.sh
```

The installer creates `~/Rumil`, imports your Zotero library, registers the MCP server with the Claude desktop app, and starts the library window in the background. If Claude Code is installed, it offers to connect Rumil there too. Quit and reopen Claude afterwards.

On Linux or Windows, follow the manual steps in [GETTING-STARTED.md](GETTING-STARTED.md#linux-or-windows). Those systems are untested.

**Updating.** Run `~/Rumil/.venv/bin/rumil update`. It installs the latest release from GitHub, refreshes the browser button and the Claude skill, and restarts the library window; your library, settings, and styles are kept. `rumil update --check` only reports whether a new release exists, and `rumil doctor` mentions it too. Running `bash install.sh` from a newer download also works.

Then, once:

```bash
~/Rumil/.venv/bin/rumil verify --all --fix
```

This checks every reference. A library of about 1,200 items takes several minutes. Then review what it found with `rumil search --status differs --limit 500` (or the "Needs attention" view in the library window).

**Two settings matter for verification.** The installer asks for both; you can also set them later:

```bash
~/Rumil/.venv/bin/rumil config set contact_email you@university.edu
~/Rumil/.venv/bin/rumil config set ads_token <token>
```

- `contact_email` puts Rumil in Crossref's "polite" pool. Without it Crossref treats Rumil as an anonymous client: lookups are limited to 5 per second and searches to 1 per second, and a busy run gets rate-limited. Rumil slows down to whatever limit Crossref announces and records rate-limited checks as errors (not "unverified"), so `rumil verify --unchecked` retries them later.
- `ads_token` (free, from https://ui.adsabs.harvard.edu/user/settings/token) lets Rumil verify conference abstracts (AGU, DPS, GSA), NASA reports, theses, and other items without a DOI. It also supplies two things Crossref lacks: the citation numbers of AGU papers from roughly 2000 to 2012 (E10007, L09202), and abstracts that Claude uses to check what a citation claims. LPSC abstracts are also checked against the LPI abstract server, which needs no token.

For AGU papers from roughly 2000 to 2012, Crossref and Zotero record AGU's manuscript number (2006JE002868) where the citation number (E10007) belongs. Rumil flags these, replaces them from NASA ADS when it has a token, and leaves them out of the .bib files it writes rather than print a wrong number.

`rumil doctor` lists any setting that is missing. Restart the Claude app (and run `rumil install-service` to restart the library window) after changing them.

**Claude skill.** The `rumil-citations` skill tells Claude how to cite with Rumil (never from memory, how to handle each verification status, how to cite in LaTeX and Word). The installer saves it as `~/Rumil/rumil-citations-skill.zip`. In Claude, open Settings > Capabilities > Skills, click "Upload skill", and choose that file. For Claude Code, the installer offers to copy it into `~/.claude/skills/`.

**Claude Code:** the installer offers to connect it. To do it by hand: `claude mcp add --scope user rumil -- ~/Rumil/.venv/bin/rumil mcp`

**Chrome extension:** chrome://extensions, then turn on Developer mode, click "Load unpacked", and choose `~/Rumil/chrome-extension`. The shortcut is Cmd+Shift+S.

## Command line

```
rumil import-zotero              # import / re-sync Zotero
rumil add 10.1130/G50341.1 arXiv:2208.03533 --collection "Mars crust"
rumil search author:smith basalt year:2021
rumil verify --all --fix         # or: rumil verify <citekeys>   |   --unchecked   |   --collection NAME
rumil verify --all --correct     # also apply registry corrections to confirmed references
rumil search --status differs    # references whose printed fields differ from the registry
rumil fix <citekey> [s1 s3]      # apply suggestions (default: the safe ones)
rumil fix <citekey> --corrections   |   --keep title
rumil config show                # | rumil config set contact_email you@example.edu
rumil cite --style icarus "[@smithBasalticCrust2021, p. 3]" @jonesImpactMelt2019
rumil check-tex paper.tex --write-bib rumil-refs.bib [--import-missing]
rumil render-docx draft.docx --style agu
rumil export --format biblatex --collection "Mars crust" --out refs.bib
rumil import coauthor.bib --collection "Proposal 2027"   # verifies new entries (--no-verify to skip)
rumil duplicates
rumil styles [--install journal-of-geophysical-research-planets]
rumil serve --open               # library window in the foreground
rumil doctor                     # settings, registry connectivity, available updates
rumil update                     # install the latest release (--check to only look)
```

## Files

```
~/Rumil/
  library.sqlite     the library (SQLite; one daily backup kept in backups/ for 14 days)
  library.bib        whole library as BibTeX, kept current automatically
  library.json       whole library as CSL-JSON (for pandoc)
  config.json        settings
  styles/            your CSL styles
  storage/           PDFs Rumil downloads (Zotero PDFs stay in ~/Zotero/storage)
  chrome-extension/  the browser button
  rumil-citations-skill.zip   the Claude skill, ready to upload
```

Settings in `config.json`: `contact_email`, `ads_token`, `zotero_dir`, `default_style`, `auto_export`, `bibtex_ascii` (write accents as LaTeX macros for old .bst files), `protected_words` (extra words to brace in BibTeX titles), `web_port`, `verify_concurrency`.

## Uninstall

```bash
~/Rumil/.venv/bin/rumil uninstall-service
```

Then remove the `rumil` entry from the Claude desktop app's `claude_desktop_config.json` (on macOS, in `~/Library/Application Support/Claude/`; on Windows, in `%APPDATA%\Claude\`), run `claude mcp remove --scope user rumil` if you use Claude Code, and delete `~/Rumil`. Your Zotero library is never modified by Rumil.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```

The tests replay recorded registry responses (`tests/fixtures`), so they run offline. GitHub runs them on macOS and Linux with Python 3.10 and 3.13 for every change. [CONTRIBUTING.md](CONTRIBUTING.md) covers reporting problems, proposing fixes, and making a release.

Built with citeproc-py (CSL formatting), python-docx, bibtexparser, pypdf, Starlette, and the MCP Python SDK. CSL styles are from the Citation Style Language project (CC BY-SA 3.0).

## License and contact

Rumil is released under the MIT License (see [LICENSE](LICENSE)); third-party parts are listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). Report problems and ask questions at https://github.com/michael-s-phillips/rumil/issues, with the output of `rumil doctor`. Changes are listed in [CHANGELOG.md](CHANGELOG.md), and [CONTRIBUTING.md](CONTRIBUTING.md) explains how to propose fixes.
