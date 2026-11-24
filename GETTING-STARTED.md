# Getting started with Rumil

Rumil is a citation library that Claude can use directly. It imports your Zotero library, checks every reference against the registry that issued it (Crossref, DataCite, arXiv, NASA ADS, and others), and formats citations for LaTeX and Word, so that Claude cites from verified records instead of from memory. Everything runs on your own computer, and Rumil never changes your Zotero library.

## What you need

- A Mac with Python 3.10 or newer. Rumil has been tested on macOS; Linux and Windows steps are at the end of this guide but are untested.
- The Claude desktop app, Claude Code, or both. Uploading the citation skill requires a Pro, Max, Team, or Enterprise plan.
- Zotero is optional. Without it, you start with an empty library and add references by DOI, by .bib file, or from the browser button.

## Install on a Mac

1. Download `rumil-vX.Y.Z.zip` from https://github.com/michael-s-phillips/rumil/releases/latest (under "Assets") and unzip it, for example in Downloads.
2. In Terminal, run:

   ```bash
   cd ~/Downloads/rumil
   bash install.sh
   ```

3. The installer asks for two settings:
   - **Contact email.** Enter your own email address. Crossref gives faster, more reliable service to clients that identify themselves, and it uses the address to contact whoever is making the requests if something goes wrong, so use your own.
   - **NASA ADS token.** Optional but recommended for planetary science. Get a free token at https://ui.adsabs.harvard.edu/user/settings/token (sign in, then click "Generate a new key"). With it, Rumil can verify AGU and GSA abstracts, NASA reports, theses, and other works without DOIs, and it can fill in the citation numbers of older AGU papers (such as E10007) and abstracts that Crossref lacks.
4. The installer then imports your Zotero library, connects Rumil to the Claude desktop app, and starts the library window at http://127.0.0.1:23120. If you use Claude Code, it offers to connect Rumil there too.
5. Quit and reopen the Claude desktop app.

You can change either setting later with `~/Rumil/.venv/bin/rumil config set contact_email you@university.edu` or `~/Rumil/.venv/bin/rumil config set ads_token <token>`.

## Add the citation skill to Claude

The skill tells Claude how to use Rumil: never cite from memory, look in your library first, and show you any reference whose details disagree with the registry.

1. In Claude, open Settings > Capabilities and make sure code execution is turned on.
2. In the Skills section, click "Upload skill" and choose `~/Rumil/rumil-citations-skill.zip`.

On an Enterprise plan, an organization owner may need to turn on Skills first. Team and Enterprise owners can also provide the skill to everyone in the organization from the admin settings.

## Check your library once

Run this once after installing. A library of about 1,200 references takes several minutes.

```bash
~/Rumil/.venv/bin/rumil verify --all --fix
```

The `--fix` option applies only safe fixes: filling empty fields from the registry and removing junk such as "(Author abstract)" tags or "n/a" page numbers. It never replaces anything you typed. Then open the library window and click "Needs attention." References marked "differs from registry" have a title word, author name, volume, issue, or page number that disagrees with the published record. For each one, click "Use registry values" to correct it, or "Keep mine" if your version is deliberate.

## Using Rumil with Claude

Ask Claude in plain language. Some examples:

- "Find the paper on Hellas rim feldspar from 2022 and add it to my library."
- "Cite these three papers in JGR Planets style and give me the reference list."
- "Audit paper.tex with Rumil and import any references that are only in its .bib file."
- "Format the citations in draft.docx in Icarus style."
- "Format these references for an LPSC abstract."

For LaTeX, `~/Rumil/library.bib` always holds your whole library, and `rumil check-tex paper.tex --write-bib rumil-refs.bib` writes a .bib with just the references a paper cites.

## The browser button (optional)

The "Save to Rumil" button adds the paper you're reading in Chrome. In Chrome, open chrome://extensions, turn on Developer mode, click "Load unpacked", and choose `~/Rumil/chrome-extension`. Computers managed by an IT department sometimes block Developer mode; in that case, add papers through Claude or the library window instead.

## Your data

Your library, settings, and PDFs stay in the `~/Rumil` folder on your computer. Rumil contacts the registries only to look up DOIs, titles, and authors of references you are checking. Your contact email goes to those services so that Crossref can identify the client making requests.

## Linux or Windows

These steps are untested. You need Python 3.10 or newer.

```bash
python3 -m venv ~/Rumil/.venv
~/Rumil/.venv/bin/pip install ~/Downloads/rumil    # the unzipped release folder
~/Rumil/.venv/bin/rumil init --email you@university.edu
~/Rumil/.venv/bin/rumil import-zotero
~/Rumil/.venv/bin/rumil install-claude
~/Rumil/.venv/bin/rumil serve --open
```

On Windows, the commands are in `%USERPROFILE%\Rumil\.venv\Scripts\` instead of `~/Rumil/.venv/bin/`. On Windows, quit Zotero before running `import-zotero`. The library window doesn't start automatically on Linux or Windows, so run `rumil serve` when you want it. For the skill, upload `skill/rumil-citations.zip` from the unzipped download folder.

## Updating

Run `~/Rumil/.venv/bin/rumil update`. It installs the newest release from GitHub and restarts the library window; your library and settings are kept. Afterward, quit and reopen the Claude desktop app. To see whether an update exists without installing it, run `rumil update --check`.

## Questions or problems

Open an issue at https://github.com/michael-s-phillips/rumil/issues (a free GitHub account is needed). Please include the output of `~/Rumil/.venv/bin/rumil doctor`, which lists your settings and checks the connections to each registry, and leave out your ADS token if you paste your settings.

Rumil is released under the MIT License (see LICENSE).
