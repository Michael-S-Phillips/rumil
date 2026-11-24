#!/bin/bash
# Rumil installer for macOS.
# Run from the unzipped folder in Terminal:   bash install.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
export RUMIL_HOME="${RUMIL_HOME:-$HOME/Rumil}"
export RUMIL_INSTALLER=1
VENV="$RUMIL_HOME/.venv"

say() { printf "\n\033[1m%s\033[0m\n" "$*"; }

pick_python() {
  for c in python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 \
           "$HOME/anaconda3/bin/python3" "$HOME/miniconda3/bin/python3" python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      command -v "$c"; return 0
    fi
  done
  return 1
}

PY=$(pick_python) || { echo "Rumil needs Python 3.10 or newer (python.org installer or 'brew install python')."; exit 1; }
say "Installing Rumil into $RUMIL_HOME (using $("$PY" --version 2>&1))"
mkdir -p "$RUMIL_HOME"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet --upgrade "$SRC"
rm -rf "$RUMIL_HOME/chrome-extension"
cp -R "$SRC/chrome-extension" "$RUMIL_HOME/chrome-extension"
# The citation skill, zipped for upload in Claude's Settings > Capabilities > Skills
(cd "$SRC/skill" && "$VENV/bin/python" -c "
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], 'w', zipfile.ZIP_DEFLATED) as z:
    z.write('rumil-citations/SKILL.md')
" "$RUMIL_HOME/rumil-citations-skill.zip")

say "Settings"
FRESH=""
[ -f "$RUMIL_HOME/config.json" ] || FRESH=1
"$VENV/bin/rumil" init
if [ -n "$FRESH" ]; then
  echo "Default citation style, used when you don't name one (change it any time in the library window)."
  echo "  For example: agu (JGR, GRL), apa, chicago, nature, science, ieee, icarus, gsa, aas, mdpi, lpsc,"
  echo "  or any style id from https://www.zotero.org/styles"
  STYLE=""
  read -r -p "Default style [agu]: " STYLE || true
  if [ -n "$STYLE" ]; then
    if "$VENV/bin/python" -c 'import sys; from rumil.config import Config; from rumil.formatting import StyleRegistry
StyleRegistry(Config()).get(sys.argv[1])' "$STYLE" >/dev/null 2>&1; then
      "$VENV/bin/rumil" config set default_style "$STYLE" >/dev/null && echo "Default style: $STYLE"
    else
      echo "Couldn't find a style called '$STYLE'; keeping AGU. Change it later with: rumil config set default_style <id>"
    fi
  fi
fi
EMAIL=""
if [ "$("$VENV/bin/rumil" config get contact_email)" = '""' ]; then
  echo "Without a contact email, Crossref treats Rumil as an anonymous client and rate-limits it."
  read -r -p "Contact email for Crossref's 'polite' pool (press Return to skip): " EMAIL || true
  if [ -n "$EMAIL" ]; then "$VENV/bin/rumil" config set contact_email "$EMAIL" >/dev/null && echo "Contact email saved."; fi
else
  echo "Contact email: already set."
fi
if [ "$("$VENV/bin/rumil" config get ads_token)" = '""' ]; then
  echo "A free NASA ADS token lets Rumil check AGU/GSA abstracts, reports, theses, and other works without DOIs."
  echo "Get one at https://ui.adsabs.harvard.edu/user/settings/token (sign in, then Generate a new key)."
  ADS=""
  read -r -p "ADS token (optional; press Return to skip): " ADS || true
  if [ -n "$ADS" ]; then "$VENV/bin/rumil" config set ads_token "$ADS" >/dev/null && echo "ADS token saved."; fi
else
  echo "ADS token: already set."
fi

ZDIR="$("$VENV/bin/python" -c 'from rumil.config import Config; print(Config().zotero_dir)')"
if [ -f "$ZDIR/zotero.sqlite" ]; then
  say "Importing your Zotero library"
  "$VENV/bin/rumil" import-zotero || echo "Zotero import failed; you can retry later with: rumil import-zotero"
else
  say "No Zotero library found in $ZDIR"
  echo "Skipping the import. You can add references by DOI, from a .bib file, or with the browser button."
  echo "If your Zotero data is elsewhere: rumil config set zotero_dir /path/to/Zotero, then rumil import-zotero"
fi

say "Connecting Rumil to the Claude desktop app"
"$VENV/bin/rumil" install-claude

if command -v claude >/dev/null 2>&1 || [ -d "$HOME/.claude" ]; then
  say "Claude Code"
  read -r -p "Also connect Rumil to Claude Code and install its citation skill there? [Y/n] " CC || true
  if [[ ! "${CC:-}" =~ ^[Nn] ]]; then
    if command -v claude >/dev/null 2>&1; then
      claude mcp remove --scope user rumil >/dev/null 2>&1 || true
      claude mcp add --scope user rumil -- "$VENV/bin/rumil" mcp && echo "Rumil tools added to Claude Code."
    else
      echo "The 'claude' command isn't on your PATH; add the tools later with:"
      echo "  claude mcp add --scope user rumil -- $VENV/bin/rumil mcp"
    fi
    mkdir -p "$HOME/.claude/skills/rumil-citations"
    cp "$SRC/skill/rumil-citations/SKILL.md" "$HOME/.claude/skills/rumil-citations/SKILL.md"
    echo "Citation skill installed in ~/.claude/skills/rumil-citations."
  fi
fi

say "Starting the library window in the background (http://127.0.0.1:23120)"
"$VENV/bin/rumil" install-service

say "Checking connections to Crossref, DataCite, arXiv, and Open Library"
"$VENV/bin/rumil" doctor || true

ZRC="$HOME/.zshrc"
# An alias, not a PATH entry: putting the venv's bin folder on PATH would also put its python and pip ahead of
# conda's or Homebrew's.
LINE="alias rumil=\"$VENV/bin/rumil\""
if ! grep -qs "alias rumil=" "$ZRC"; then
  read -r -p "Add a 'rumil' command for Terminal (~/.zshrc)? [y/N] " ADD || true
  if [[ "${ADD:-}" =~ ^[Yy] ]]; then echo "$LINE" >> "$ZRC"; echo "Added. Open a new Terminal window to use 'rumil'."; fi
fi

cat <<MSG

Rumil is installed.

  1. Quit and reopen the Claude desktop app so it loads the Rumil tools.
     Then add the citation skill: in Claude, open Settings > Capabilities > Skills,
     click "Upload skill", and choose  $RUMIL_HOME/rumil-citations-skill.zip
  2. Open your library:  http://127.0.0.1:23120
  3. Verify everything once (takes a few minutes):  $VENV/bin/rumil verify --all --fix
     (or click Verify in the library window)
  4. Browser button: in Chrome open chrome://extensions, turn on Developer mode,
     click "Load unpacked", and choose  $RUMIL_HOME/chrome-extension
  5. LaTeX: \\bibliography{$RUMIL_HOME/library} always holds your whole library,
     or run 'rumil check-tex paper.tex --write-bib rumil-refs.bib' for a per-paper .bib.

MSG
