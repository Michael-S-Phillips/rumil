"""Configuration and on-disk layout for a Rumil library.

Everything lives under one folder (``~/Rumil`` by default, override with the
``RUMIL_HOME`` environment variable):

    ~/Rumil/
        library.sqlite      the library database
        config.json         settings (this module)
        library.bib         auto-exported BibTeX of the whole library
        library.json        auto-exported CSL-JSON of the whole library
        styles/             your CSL styles (copied from Zotero at setup)
        storage/            PDFs saved by Rumil itself (Zotero PDFs stay in ~/Zotero)
        backups/            daily database snapshots
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    # Joining Crossref's "polite pool" (faster, more reliable) only needs a contact
    # email. Leave blank to use the public pool.
    "contact_email": "",
    # Optional NASA ADS API token (https://ui.adsabs.harvard.edu/user/settings/token).
    # With it, Rumil can verify LPSC abstracts and other items that have no DOI.
    "ads_token": "",
    "zotero_dir": "~/Zotero",
    "default_style": "american-geophysical-union",
    # Paths are relative to the Rumil folder unless absolute. Empty string disables.
    "auto_export": {
        "bibtex": "library.bib",
        "biblatex": "",
        "csljson": "library.json",
    },
    # BibTeX export: write accented characters as LaTeX macros instead of UTF-8.
    "bibtex_ascii": False,
    # Extra words to brace-protect in BibTeX titles (proper nouns your .bst would
    # otherwise lowercase). Planetary names are already built in.
    "protected_words": [],
    "web_port": 23120,
    # Browser extensions allowed to call the local app (the bundled "Save to Rumil" extension's fixed id).
    "allowed_extensions": ["melhjdcjdcnjigajdciofmkijnehmcjm"],
    "backup_keep": 14,
    "verify_concurrency": 3,
    # Where `rumil update` looks for new releases (a GitHub "owner/repository").
    "update_repo": "michael-s-phillips/rumil",
}


SECRET_KEYS = {"ads_token"}


def setup_warnings(cfg: "Config") -> list[str]:
    """Settings that limit what Rumil can check, with the command that fixes each."""
    out = []
    if not (cfg.get("contact_email") or "").strip():
        out.append("No contact email: Crossref treats Rumil as anonymous (1 search per second, frequent rate limits). "
                   "Fix: rumil config set contact_email you@example.edu")
    if not (cfg.get("ads_token") or "").strip():
        out.append("No NASA ADS token: conference abstracts (LPSC, AGU, GSA) and other works without DOIs can't be "
                   "checked against ADS. Get a free token at https://ui.adsabs.harvard.edu/user/settings/token, then: "
                   "rumil config set ads_token <token>")
    return out


def rumil_home() -> Path:
    return Path(os.environ.get("RUMIL_HOME", "~/Rumil")).expanduser()


class Config:
    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else rumil_home()
        self.path = self.home / "config.json"
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            try:
                user = json.loads(self.path.read_text(encoding="utf-8"))
                _deep_update(self.data, user)
            except (OSError, json.JSONDecodeError):
                pass

    # -- access -------------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    # -- paths --------------------------------------------------------------
    def resolve(self, p: str | os.PathLike | None) -> Path | None:
        if not p:
            return None
        path = Path(os.path.expanduser(str(p)))
        return path if path.is_absolute() else self.home / path

    @property
    def db_path(self) -> Path:
        return self.home / "library.sqlite"

    @property
    def styles_dir(self) -> Path:
        return self.home / "styles"

    @property
    def storage_dir(self) -> Path:
        return self.home / "storage"

    @property
    def backups_dir(self) -> Path:
        return self.home / "backups"

    @property
    def zotero_dir(self) -> Path:
        return Path(os.path.expanduser(self.data.get("zotero_dir") or "~/Zotero"))


def _deep_update(base: dict, new: dict) -> None:
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
