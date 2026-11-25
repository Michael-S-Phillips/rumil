# Changelog

## [Unreleased]

- A registry record with no title (some Crossref records have none) is no longer reported as "metadata mismatch"; the title is noted as unchecked, and "Keep mine" for the title now works instead of failing with "The registry record has no 'title' to differ from".
- A kept author list now stays kept when the registry's list includes an organisation (for example a GSA record that lists a college as an author); it was flagged again on every verify.

## [0.3.0] - 2026-11-24

Rumil now lives on GitHub (https://github.com/michael-s-phillips/rumil), so anyone can install and update it.

- New `rumil update` command: installs the latest GitHub release, refreshes the browser button and the Claude skill, and restarts the library window. Your library and settings are kept. `rumil update --check` only looks, `rumil doctor` reports available updates, and Claude mentions them when it checks the library's status.
- The installer asks new users for their default citation style instead of assuming AGU.
- The macOS background service has a neutral name (`local.rumil.library`); installing or updating removes the old one.
- Problem reports go to GitHub issues instead of a personal email address.
- Releases are built and published automatically when a version tag is pushed, and every change is tested on macOS and Linux with Python 3.10 and 3.13.

## [0.2.4] - 2026-11-23

- Library window: every pane scrolls again. The three-pane layout had no height limit, so the panes grew past the window and nothing could scroll.
- In windows narrower than 1000 pixels, the details panel can be closed with its × button or Esc.
- After an update, the browser always loads the new page code instead of a cached copy.

## [0.2.3] - 2026-11-01

- DOIs containing `<`, `>`, and `;` (older GSA, AGU, Elsevier, and Wiley DOIs such as `10.1130/0091-7613(2001)029<0987:HSESAT>2.0.CO;2`) are no longer cut off at `<`.
- DOIs that older versions cut off are repaired, either by re-importing from Zotero or by a safe fix offered during verification.

## [0.2.2] - 2026-10-31

- An erratum's or retraction notice's DOI is never offered as the article's DOI.
- Names with Jr., Sr., or III are parsed correctly in every common position.
- A family name that would print differently ("Deanne Rogers" vs. "Rogers") is reported as a difference.
- Applying safe fixes repeats when one fix reveals another (a found DOI, then the full page range).
- `check-tex` lists verified references that would still print incompletely.

## [0.2.1] - 2026-10-30

- Crossref title searches work again (they asked for a field Crossref rejects).
- AGU manuscript numbers (2006JE002868) are no longer used as citation numbers; with an ADS token, the real citation number (E10007) and missing abstracts come from NASA ADS.
- Verification restores lost accents and fills in full given names from the registry.
- `check-tex` compares the paper's own .bib with the library entry by entry.
- A compact LPSC abstract style (`lpsc`).

## [0.2.0] - 2026-10-30

- Titles are compared with the registry word for word, with a new status, "differs from registry", and one-click corrections or "keep mine".
- Export debris (database tags, journal running heads, "n/a" pages) is cleaned up as a safe fix.
- Settings for a contact email and an ADS token, with warnings when they're missing.
- MIT License and a setup guide for colleagues.

## [0.1.0] - 2026-10-29

- First version: Zotero import and re-sync, verification against Crossref, DataCite, arXiv, ISBN services, PubMed, and NASA ADS, citation formatting in any CSL style, LaTeX and Word support, the library window, the browser button, and the MCP server for Claude.
