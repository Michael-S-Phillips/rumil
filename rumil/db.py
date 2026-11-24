"""The Rumil library: a single SQLite database holding CSL-JSON items plus
collections, tags, notes, attachments, citation-key aliases, verification
results, and an edit history.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import citekeys
from .config import Config
from .csl import clean_item, container_of, creators, family_of, name_str, year_of
from .ids import doi_key, normalize_arxiv, normalize_isbn
from .textutil import name_key, norm_title, strip_markup, title_similarity

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    citekey TEXT NOT NULL UNIQUE COLLATE NOCASE,
    type TEXT NOT NULL,
    csl TEXT NOT NULL,
    title TEXT, title_norm TEXT, first_family TEXT, year INTEGER, creators TEXT, container TEXT,
    doi TEXT, arxiv TEXT, isbn TEXT, url TEXT,
    source TEXT,
    date_added TEXT NOT NULL,
    date_modified TEXT NOT NULL,
    local_modified TEXT,
    verify_status TEXT NOT NULL DEFAULT 'unchecked',
    verify_checked TEXT,
    verify_details TEXT,
    suggestions TEXT,
    accepted TEXT,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_doi ON items(doi);
CREATE INDEX IF NOT EXISTS items_arxiv ON items(arxiv);
CREATE INDEX IF NOT EXISTS items_title_norm ON items(title_norm);
CREATE INDEX IF NOT EXISTS items_ff_year ON items(first_family, year);
CREATE INDEX IF NOT EXISTS items_status ON items(verify_status);
CREATE TABLE IF NOT EXISTS aliases (
    alias TEXT PRIMARY KEY COLLATE NOCASE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS zotero_map (
    zotero_library INTEGER NOT NULL,
    zotero_key TEXT NOT NULL,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    zotero_modified TEXT,
    synced_at TEXT,
    zotero_csl TEXT,
    zotero_tags TEXT,
    zotero_colls TEXT,
    zotero_citekey TEXT,
    PRIMARY KEY (zotero_library, zotero_key)
);
CREATE TABLE IF NOT EXISTS zotero_tombstones (
    kind TEXT NOT NULL,
    zotero_library INTEGER NOT NULL,
    zotero_key TEXT NOT NULL,
    ts TEXT,
    PRIMARY KEY (kind, zotero_library, zotero_key)
);
CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
    zotero_library INTEGER, zotero_key TEXT, zotero_name TEXT, zotero_parent INTEGER,
    date_added TEXT
);
CREATE TABLE IF NOT EXISTS collection_items (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    PRIMARY KEY (collection_id, item_id)
);
CREATE TABLE IF NOT EXISTS tags (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (item_id, tag)
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
    html TEXT NOT NULL,
    date_added TEXT, date_modified TEXT,
    zotero_library INTEGER, zotero_key TEXT, zotero_html TEXT
);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    title TEXT, path TEXT, url TEXT, content_type TEXT, link_mode TEXT,
    fulltext_path TEXT,
    zotero_library INTEGER, zotero_key TEXT,
    date_added TEXT
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER,
    ts TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS history_item ON history(item_id);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    citekey, title, creators, container, year, abstract, tags, notes, ids,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

FTS_COLUMNS = {"key": "citekey", "citekey": "citekey", "title": "title", "author": "creators",
               "creator": "creators", "journal": "container", "container": "container", "year": "year",
               "abstract": "abstract", "tag": "tags", "note": "notes", "doi": "ids", "id": "ids"}

STATUS_GROUPS = {
    "ok": ("verified", "verified_no_doi", "url_ok"),
    "attention": ("differs", "mismatch", "doi_found", "unverified", "url_dead"),
    "problem": ("retracted", "doi_invalid"),
    "unchecked": ("unchecked", "error"),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class NotFound(KeyError):
    pass


class Library:
    def __init__(self, cfg: Config | None = None, db_path: str | Path | None = None):
        self.cfg = cfg or Config()
        self.path = Path(db_path) if db_path else self.cfg.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 15000")
        try:
            self.conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass
        self.conn.executescript(SCHEMA)
        self._migrate()
        try:
            self.conn.executescript(FTS_SCHEMA)
            self.has_fts = True
        except sqlite3.OperationalError:
            self.has_fts = False
        if not self.get_meta("schema_version"):
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            self.set_meta("created", now_iso())

    def _migrate(self) -> None:
        """Add columns introduced after a library was created."""
        wanted = {
            "items": ["accepted TEXT"],
            "zotero_map": ["zotero_csl TEXT", "zotero_tags TEXT", "zotero_colls TEXT", "zotero_citekey TEXT"],
            "collections": ["zotero_name TEXT", "zotero_parent INTEGER"],
            "notes": ["zotero_html TEXT"],
        }
        for table, cols in wanted.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col in cols:
                if col.split()[0] not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")

    def _tombstone(self, kind: str, zlib, zkey) -> None:
        """Remember Zotero objects deleted in Rumil so a re-sync doesn't bring them back."""
        if zlib is not None and zkey:
            self.conn.execute("INSERT OR IGNORE INTO zotero_tombstones(kind, zotero_library, zotero_key, ts) VALUES (?,?,?,?)",
                              (kind, zlib, zkey, now_iso()))

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------------ tx
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self.conn.in_transaction:  # nested: join the outer transaction
                yield self.conn
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'data_version'")
                if self.conn.execute("SELECT changes()").fetchone()[0] == 0:
                    self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('data_version', '1')")
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def q(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def q1(self, sql: str, params: Iterable = ()):
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        r = self.q1("SELECT value FROM meta WHERE key = ?", (key,))
        return r[0] if r else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                              (key, value))

    @property
    def data_version(self) -> int:
        return int(self.get_meta("data_version", "0") or 0)

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _index_cols(csl: dict) -> dict:
        c = creators(csl)
        return {
            "type": csl.get("type", "document"),
            "title": strip_markup(csl.get("title", "")),
            "title_norm": norm_title(csl.get("title", "")),
            "first_family": name_key(family_of(c[0])) if c else "",
            "year": year_of(csl),
            "creators": "; ".join(name_str(n, initials=False) for n in c),
            "container": container_of(csl),
            "doi": doi_key(csl.get("DOI")) or None,
            "arxiv": _arxiv_of(csl),
            "isbn": normalize_isbn((csl.get("ISBN") or "").split(" ")[0].split(",")[0]) if csl.get("ISBN") else None,
            "url": csl.get("URL"),
        }

    def _reindex(self, item_id: int) -> None:
        if not self.has_fts:
            return
        r = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        self.conn.execute("DELETE FROM fts WHERE rowid = ?", (item_id,))
        if not r or r["deleted"]:
            return
        csl = json.loads(r["csl"])
        tags = " ".join(t[0] for t in self.conn.execute("SELECT tag FROM tags WHERE item_id = ?", (item_id,)))
        notes = " ".join(strip_markup(n[0])[:4000] for n in self.conn.execute("SELECT html FROM notes WHERE item_id = ?", (item_id,)))
        ids = " ".join(x for x in [csl.get("DOI"), r["arxiv"], r["isbn"], csl.get("PMID"), csl.get("URL")] if x)
        aliases = " ".join(a[0] for a in self.conn.execute("SELECT alias FROM aliases WHERE item_id = ?", (item_id,)))
        self.conn.execute(
            "INSERT INTO fts(rowid, citekey, title, creators, container, year, abstract, tags, notes, ids) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (item_id, f"{r['citekey']} {aliases}".strip(), r["title"], r["creators"], r["container"], str(r["year"] or ""),
             strip_markup(csl.get("abstract", ""))[:6000], tags, notes, ids),
        )

    def rebuild_index(self) -> None:
        with self.tx():
            if self.has_fts:
                self.conn.execute("DELETE FROM fts")
            for (iid,) in self.conn.execute("SELECT id FROM items").fetchall():
                self._reindex(iid)

    def log(self, item_id: int | None, action: str, detail: Any = None, actor: str = "user") -> None:
        self.conn.execute("INSERT INTO history(item_id, ts, actor, action, detail) VALUES (?,?,?,?,?)",
                          (item_id, now_iso(), actor, action,
                           json.dumps(detail, ensure_ascii=False) if detail is not None and not isinstance(detail, str) else detail))

    def _key_exists(self, key: str) -> bool:
        return bool(self.conn.execute("SELECT 1 FROM items WHERE citekey = ? UNION SELECT 1 FROM aliases WHERE alias = ?",
                                      (key, key)).fetchone())

    def _row(self, r: sqlite3.Row, full: bool = False) -> dict:
        d = {
            "id": r["id"], "citekey": r["citekey"], "csl": json.loads(r["csl"]), "source": r["source"],
            "date_added": r["date_added"], "date_modified": r["date_modified"], "local_modified": r["local_modified"],
            "verify_status": r["verify_status"], "verify_checked": r["verify_checked"],
            "verify_details": json.loads(r["verify_details"]) if r["verify_details"] else None,
            "suggestions": json.loads(r["suggestions"]) if r["suggestions"] else [],
            "accepted": json.loads(r["accepted"]) if r["accepted"] else {},
            "deleted": bool(r["deleted"]),
        }
        if full:
            iid = r["id"]
            d["tags"] = [t[0] for t in self.conn.execute("SELECT tag FROM tags WHERE item_id = ? ORDER BY tag", (iid,))]
            d["collections"] = [dict(id=c[0], name=c[1], path=self._collection_path(c[0])) for c in self.conn.execute(
                "SELECT c.id, c.name FROM collections c JOIN collection_items ci ON ci.collection_id = c.id WHERE ci.item_id = ? ORDER BY c.name",
                (iid,))]
            d["aliases"] = [a[0] for a in self.conn.execute("SELECT alias FROM aliases WHERE item_id = ?", (iid,))]
            d["notes"] = [dict(n) for n in self.conn.execute(
                "SELECT id, html, date_added, date_modified FROM notes WHERE item_id = ? ORDER BY date_added", (iid,))]
            d["attachments"] = [dict(a) for a in self.conn.execute(
                "SELECT id, title, path, url, content_type, link_mode, fulltext_path FROM attachments WHERE item_id = ?", (iid,))]
            d["zotero"] = [dict(z) for z in self.conn.execute(
                "SELECT zotero_library, zotero_key, zotero_modified, synced_at FROM zotero_map WHERE item_id = ?", (iid,))]
        return d

    # ---------------------------------------------------------------- lookup
    def resolve_id(self, ref: Any, include_deleted: bool = False) -> int | None:
        """ref may be an item id (int / 'id:12'), a citekey, or a former citekey (alias)."""
        with self._lock:
            if isinstance(ref, int):
                r = self.conn.execute("SELECT id, deleted FROM items WHERE id = ?", (ref,)).fetchone()
            else:
                s = str(ref).strip().lstrip("@")
                if s.startswith("id:") and s[3:].isdigit():
                    r = self.conn.execute("SELECT id, deleted FROM items WHERE id = ?", (int(s[3:]),)).fetchone()
                else:
                    r = self.conn.execute("SELECT id, deleted FROM items WHERE citekey = ?", (s,)).fetchone()
                    if not r:
                        r = self.conn.execute(
                            "SELECT i.id, i.deleted FROM aliases a JOIN items i ON i.id = a.item_id WHERE a.alias = ?", (s,)).fetchone()
            if not r or (r["deleted"] and not include_deleted):
                return None
            return r["id"]

    def get(self, ref: Any, full: bool = True, include_deleted: bool = False) -> dict | None:
        iid = self.resolve_id(ref, include_deleted=include_deleted)
        if iid is None:
            return None
        with self._lock:
            r = self.conn.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
            return self._row(r, full=full)

    def require(self, ref: Any, full: bool = True) -> dict:
        item = self.get(ref, full=full)
        if item is None:
            raise NotFound(f"No item with citation key or id '{ref}' in the library")
        return item

    def iter_items(self, include_deleted: bool = False, ids: Iterable[int] | None = None) -> Iterator[dict]:
        with self._lock:
            if ids is not None:
                ids = list(ids)
                rows = []
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    rows += self.conn.execute(f"SELECT * FROM items WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM items" + ("" if include_deleted else " WHERE deleted = 0") + " ORDER BY id").fetchall()
        for r in rows:
            yield self._row(r)

    def count(self) -> int:
        return self.q1("SELECT COUNT(*) FROM items WHERE deleted = 0")[0]

    # ------------------------------------------------------------- add / edit
    def add_item(self, csl: dict, *, source: str, citekey: str | None = None, collections: Iterable = (),
                 tags: Iterable[str] = (), actor: str = "user", date_added: str | None = None,
                 verify: tuple[str, dict | None, list | None] | None = None) -> dict:
        csl = clean_item(csl)
        if not csl.get("title") and not creators(csl):
            raise ValueError("An item needs at least a title or an author")
        with self.tx():
            if citekey and citekeys.valid_key(citekey) and not self._key_exists(citekey):
                key = citekey
            else:
                key = citekeys.make_unique(citekeys.base_key(csl), self._key_exists)
            cols = self._index_cols(csl)
            ts = now_iso()
            cur = self.conn.execute(
                """INSERT INTO items (citekey, type, csl, title, title_norm, first_family, year, creators, container,
                   doi, arxiv, isbn, url, source, date_added, date_modified, local_modified)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, cols["type"], json.dumps(csl, ensure_ascii=False), cols["title"], cols["title_norm"],
                 cols["first_family"], cols["year"], cols["creators"], cols["container"], cols["doi"], cols["arxiv"],
                 cols["isbn"], cols["url"], source, date_added or ts, ts, ts if source != "zotero" else None),
            )
            iid = cur.lastrowid
            if citekey and key != citekey and citekeys.valid_key(citekey) and not self._key_exists(citekey):
                self.conn.execute("INSERT OR IGNORE INTO aliases(alias, item_id) VALUES (?, ?)", (citekey, iid))
            for t in tags or ():
                if t and t.strip():
                    self.conn.execute("INSERT OR IGNORE INTO tags(item_id, tag) VALUES (?, ?)", (iid, t.strip()))
            for c in collections or ():
                cid = self._collection_id(c, create=True)
                self.conn.execute("INSERT OR IGNORE INTO collection_items(collection_id, item_id) VALUES (?, ?)", (cid, iid))
            if verify:
                self._set_verification(iid, *verify)
            self.log(iid, "add", {"source": source, "citekey": key}, actor)
            self._reindex(iid)
            return self._row(self.conn.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone(), full=True)

    def update_item(self, ref: Any, csl: dict | None = None, *, patch: dict | None = None, actor: str = "user",
                    reason: str = "", from_sync: bool = False, reset_verification: bool = True) -> dict:
        """Replace (csl=) or patch (patch=) an item's metadata. A None value in patch deletes the field."""
        with self.tx():
            iid = self.resolve_id(ref, include_deleted=True)
            if iid is None:
                raise NotFound(f"No item '{ref}'")
            r = self.conn.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
            old = json.loads(r["csl"])
            new = dict(old) if csl is None else dict(csl)
            for k, v in (patch or {}).items():
                if v is None or v == "" or v == []:
                    new.pop(k, None)
                else:
                    new[k] = v
            new = clean_item(new)
            if new == old:
                return self._row(r, full=True)
            cols = self._index_cols(new)
            ts = now_iso()
            changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
            self.conn.execute(
                """UPDATE items SET type=?, csl=?, title=?, title_norm=?, first_family=?, year=?, creators=?, container=?,
                   doi=?, arxiv=?, isbn=?, url=?, date_modified=?, local_modified=COALESCE(?, local_modified)
                   WHERE id=?""",
                (cols["type"], json.dumps(new, ensure_ascii=False), cols["title"], cols["title_norm"], cols["first_family"],
                 cols["year"], cols["creators"], cols["container"], cols["doi"], cols["arxiv"], cols["isbn"], cols["url"],
                 ts, None if from_sync else ts, iid),
            )
            if reset_verification and any(k in changed for k in ("title", "author", "issued", "DOI", "container-title", "type",
                                                                    "volume", "issue", "page", "URL", "ISBN", "editor")):
                self.conn.execute("UPDATE items SET verify_status='unchecked', suggestions=NULL WHERE id=?", (iid,))
            self.log(iid, "sync-update" if from_sync else "edit",
                     {"reason": reason, "changed": changed, "before": {k: old.get(k) for k in changed}}, actor)
            self._reindex(iid)
            return self._row(self.conn.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone(), full=True)

    def set_citekey(self, ref: Any, new_key: str, actor: str = "user") -> dict:
        new_key = new_key.strip()
        if not citekeys.valid_key(new_key):
            raise ValueError(f"'{new_key}' is not a valid citation key (letters, digits, and _:.-+/ only)")
        with self.tx():
            iid = self.resolve_id(ref)
            if iid is None:
                raise NotFound(f"No item '{ref}'")
            old = self.conn.execute("SELECT citekey FROM items WHERE id = ?", (iid,)).fetchone()[0]
            if old.lower() == new_key.lower():
                self.conn.execute("UPDATE items SET citekey = ? WHERE id = ?", (new_key, iid))
            else:
                owner = self.resolve_id(new_key, include_deleted=True)
                if owner is not None and owner != iid:
                    raise ValueError(f"Citation key '{new_key}' is already used by another item")
                self.conn.execute("DELETE FROM aliases WHERE alias = ?", (new_key,))
                self.conn.execute("UPDATE items SET citekey = ? WHERE id = ?", (new_key, iid))
                # The old key keeps resolving so existing manuscripts don't break.
                self.conn.execute("INSERT OR IGNORE INTO aliases(alias, item_id) VALUES (?, ?)", (old, iid))
            self.log(iid, "rename-key", {"from": old, "to": new_key}, actor)
            self._reindex(iid)
        return self.require(iid)

    def add_alias(self, ref: Any, alias: str) -> None:
        with self.tx():
            iid = self.resolve_id(ref)
            if iid is not None and citekeys.valid_key(alias) and not self._key_exists(alias):
                self.conn.execute("INSERT INTO aliases(alias, item_id) VALUES (?, ?)", (alias, iid))
                self._reindex(iid)

    def trash(self, ref: Any, actor: str = "user") -> None:
        with self.tx():
            iid = self.resolve_id(ref)
            if iid is None:
                raise NotFound(f"No item '{ref}'")
            self.conn.execute("UPDATE items SET deleted = 1, date_modified = ? WHERE id = ?", (now_iso(), iid))
            self.log(iid, "trash", None, actor)
            self._reindex(iid)

    def restore(self, ref: Any, actor: str = "user") -> None:
        with self.tx():
            iid = self.resolve_id(ref, include_deleted=True)
            if iid is None:
                raise NotFound(f"No item '{ref}'")
            self.conn.execute("UPDATE items SET deleted = 0, date_modified = ? WHERE id = ?", (now_iso(), iid))
            self.log(iid, "restore", None, actor)
            self._reindex(iid)

    def empty_trash(self, actor: str = "user") -> int:
        with self.tx():
            rows = self.conn.execute("SELECT id, citekey, csl FROM items WHERE deleted = 1").fetchall()
            for r in rows:
                for z in self.conn.execute("SELECT zotero_library, zotero_key FROM zotero_map WHERE item_id = ?", (r["id"],)).fetchall():
                    self._tombstone("item", z[0], z[1])
                self.log(None, "purge", {"citekey": r["citekey"], "csl": json.loads(r["csl"])}, actor)
                self.conn.execute("DELETE FROM fts WHERE rowid = ?", (r["id"],)) if self.has_fts else None
                self.conn.execute("DELETE FROM items WHERE id = ?", (r["id"],))
            return len(rows)

    # ------------------------------------------------------------ duplicates
    STRONG_REASONS = ("same DOI", "same arXiv id", "same ISBN", "same title, authors, year, and type")

    def find_duplicates_of(self, csl: dict, exclude_id: int | None = None,
                           strong_only: bool = False) -> list[tuple[dict, str, float]]:
        """Existing items that look like the same work as `csl`.

        Strong matches (same DOI / arXiv id / book ISBN, or an identical title with the same first author,
        year, and type) are treated as the same work. With strong_only=False, near-identical titles of
        the same type within a year are also returned as possible duplicates for a person to review."""
        cols = self._index_cols(clean_item(csl))
        found: dict[int, tuple[str, float]] = {}
        with self._lock:
            if cols["doi"]:
                for r in self.conn.execute("SELECT id FROM items WHERE doi = ? AND deleted = 0", (cols["doi"],)):
                    found[r[0]] = ("same DOI", 1.0)
            if cols["arxiv"]:
                for r in self.conn.execute("SELECT id FROM items WHERE arxiv = ? AND deleted = 0", (cols["arxiv"],)):
                    found.setdefault(r[0], ("same arXiv id", 1.0))
            if cols["isbn"] and cols["type"] == "book":
                for r in self.conn.execute("SELECT id FROM items WHERE isbn = ? AND type = 'book' AND deleted = 0", (cols["isbn"],)):
                    found.setdefault(r[0], ("same ISBN", 0.95))
            if cols["title_norm"]:
                cands = self.conn.execute(
                    "SELECT id, title, title_norm, year, first_family, doi, arxiv, type, container FROM items WHERE deleted = 0 "
                    "AND (title_norm = ? OR (first_family = ? AND first_family != ''))",
                    (cols["title_norm"], cols["first_family"])).fetchall()
                for r in cands:
                    if r["id"] in found or r["type"] != cols["type"]:
                        continue  # an LPSC abstract and the later journal paper are different works
                    # Different identifiers mean different works (e.g. a preprint and its article).
                    if (cols["doi"] and r["doi"] and r["doi"] != cols["doi"]) or \
                            (cols["arxiv"] and r["arxiv"] and r["arxiv"] != cols["arxiv"]):
                        continue
                    if _containers_conflict(cols["container"], r["container"]):
                        continue  # same talk title at LPSC and at AGU: two different abstracts
                    if (r["title_norm"] == cols["title_norm"] and r["year"] == cols["year"] and cols["year"]
                            and r["first_family"] == cols["first_family"] and cols["first_family"]):
                        found[r["id"]] = ("same title, authors, year, and type", 1.0)
                        continue
                    if strong_only:
                        continue
                    sim = title_similarity(cols["title"], r["title"])
                    year_ok = not cols["year"] or not r["year"] or abs(cols["year"] - r["year"]) <= 1
                    if sim >= 0.93 and year_ok and (not cols["first_family"] or not r["first_family"]
                                                     or r["first_family"] == cols["first_family"]):
                        found[r["id"]] = ("very similar title, same author", round(sim, 3))
        found.pop(exclude_id, None)
        out = []
        for i, (why, score) in sorted(found.items(), key=lambda kv: -kv[1][1]):
            it = self.get(i, full=False)
            if it:
                out.append((it, why, score))
        return out

    def items_with_title(self, csl: dict) -> list[dict]:
        """Items (any type) whose normalized title equals this record's."""
        cols = self._index_cols(clean_item(csl))
        if not cols["title_norm"]:
            return []
        with self._lock:
            ids = [r[0] for r in self.conn.execute("SELECT id FROM items WHERE deleted = 0 AND title_norm = ?",
                                                   (cols["title_norm"],))]
        return [it for it in (self.get(i, full=False) for i in ids) if it]

    def duplicate_groups(self) -> list[list[dict]]:
        """Groups of items that appear to be the same work."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, doi, arxiv, title, title_norm, first_family, year, type FROM items WHERE deleted = 0").fetchall()
        parent = {r["id"]: r["id"] for r in rows}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        by: dict[tuple, list] = {}
        for r in rows:
            if r["doi"]:
                by.setdefault(("doi", r["doi"]), []).append(r["id"])
            if r["arxiv"]:
                by.setdefault(("arxiv", r["arxiv"]), []).append(r["id"])
            if r["title_norm"] and len(r["title_norm"]) > 12:
                by.setdefault(("title", r["title_norm"], r["first_family"], r["type"]), []).append(r["id"])
        row_by_id = {r["id"]: r for r in rows}

        def conflict(a, b):  # different DOIs or arXiv ids: different works, whatever the titles say
            ra, rb = row_by_id[a], row_by_id[b]
            return bool((ra["doi"] and rb["doi"] and ra["doi"] != rb["doi"]) or
                        (ra["arxiv"] and rb["arxiv"] and ra["arxiv"] != rb["arxiv"]))

        for key, ids in by.items():
            for other in ids[1:]:
                if key[0] == "title" and conflict(ids[0], other):
                    continue
                union(ids[0], other)
        # Fuzzy pass within (first author, year) buckets.
        bucket: dict[tuple, list] = {}
        for r in rows:
            if r["first_family"] and r["year"]:
                bucket.setdefault((r["first_family"], r["year"], r["type"]), []).append(r)
        for rs in bucket.values():
            for i in range(len(rs)):
                for j in range(i + 1, len(rs)):
                    a, b = rs[i], rs[j]
                    if a["doi"] and b["doi"] and a["doi"] != b["doi"]:
                        continue
                    if title_similarity(a["title"], b["title"]) >= 0.93:
                        union(a["id"], b["id"])
        groups: dict[int, list[int]] = {}
        for r in rows:
            groups.setdefault(find(r["id"]), []).append(r["id"])
        out = []
        for ids in groups.values():
            if len(ids) > 1:
                out.append([self.get(i, full=False) for i in sorted(ids)])
        return out

    def merge(self, keep_ref: Any, remove_refs: Iterable[Any], actor: str = "user") -> dict:
        """Merge duplicates into one item. Removed items' citekeys become aliases of the kept item."""
        with self.tx():
            keep = self.resolve_id(keep_ref)
            if keep is None:
                raise NotFound(f"No item '{keep_ref}'")
            krow = self.conn.execute("SELECT * FROM items WHERE id = ?", (keep,)).fetchone()
            kcsl = json.loads(krow["csl"])
            merged_keys = []
            for ref in remove_refs:
                rid = self.resolve_id(ref, include_deleted=True)
                if rid is None or rid == keep:
                    continue
                rrow = self.conn.execute("SELECT * FROM items WHERE id = ?", (rid,)).fetchone()
                rcsl = json.loads(rrow["csl"])
                for k, v in rcsl.items():  # fill gaps only; never overwrite the kept record
                    if k not in kcsl and v:
                        kcsl[k] = v
                for tbl, col in (("tags", "item_id"), ("collection_items", "item_id")):
                    self.conn.execute(f"UPDATE OR IGNORE {tbl} SET {col} = ? WHERE {col} = ?", (keep, rid))
                for tbl in ("notes", "attachments", "zotero_map", "aliases"):
                    self.conn.execute(f"UPDATE OR IGNORE {tbl} SET item_id = ? WHERE item_id = ?", (keep, rid))
                self.log(keep, "merge", {"removed_citekey": rrow["citekey"], "removed_csl": rcsl}, actor)
                merged_keys.append(rrow["citekey"])
                if self.has_fts:
                    self.conn.execute("DELETE FROM fts WHERE rowid = ?", (rid,))
                self.conn.execute("DELETE FROM items WHERE id = ?", (rid,))
                self.conn.execute("INSERT OR IGNORE INTO aliases(alias, item_id) VALUES (?, ?)", (rrow["citekey"], keep))
            original = json.loads(krow["csl"])
            kcsl = clean_item(kcsl)
            cols = self._index_cols(kcsl)
            self.conn.execute(
                """UPDATE items SET csl=?, type=?, title=?, title_norm=?, first_family=?, year=?, creators=?, container=?,
                   doi=?, arxiv=?, isbn=?, url=?, date_modified=?, local_modified=? WHERE id=?""",
                (json.dumps(kcsl, ensure_ascii=False), cols["type"], cols["title"], cols["title_norm"], cols["first_family"],
                 cols["year"], cols["creators"], cols["container"], cols["doi"], cols["arxiv"], cols["isbn"], cols["url"],
                 now_iso(), now_iso(), keep))
            if kcsl != clean_item(original):
                # Fields came over from the merged items and haven't been checked yet.
                self.conn.execute("UPDATE items SET verify_status = 'unchecked', suggestions = NULL WHERE id = ?", (keep,))
            self._reindex(keep)
        result = self.require(keep)
        result["merged_citekeys"] = merged_keys
        return result

    # ----------------------------------------------------------------- search
    def search(self, query: str = "", *, collection: Any = None, tag: str | None = None, status: str | None = None,
               year_from: int | None = None, year_to: int | None = None, item_type: str | None = None,
               special: str | None = None, sort: str = "relevance", order: str = "desc",
               limit: int = 50, offset: int = 0, include_deleted: bool = False) -> tuple[list[dict], int]:
        where, params, join = [], [], ""
        rank_expr = "i.date_added"
        query = (query or "").strip()
        if special == "trash":
            where.append("i.deleted = 1")
        elif not include_deleted:
            where.append("i.deleted = 0")
        if query:
            if self.has_fts:
                fts_q = _fts_query(query)
                if fts_q:
                    join += " JOIN fts ON fts.rowid = i.id"
                    where.append("fts MATCH ?")
                    params.append(fts_q)
                    rank_expr = "bm25(fts, 6.0, 10.0, 6.0, 2.0, 3.0, 1.0, 3.0, 1.0, 6.0)"
            else:
                for tok in query.split():
                    where.append("(i.title LIKE ? OR i.creators LIKE ? OR i.citekey LIKE ? OR i.doi LIKE ?)")
                    params += [f"%{tok}%"] * 4
        if collection not in (None, "", "all"):
            cid = self._collection_id(collection)
            if cid is None:
                return [], 0
            ids = [cid] + self._descendants(cid)
            where.append(f"i.id IN (SELECT item_id FROM collection_items WHERE collection_id IN ({','.join('?' * len(ids))}))")
            params += ids
        if tag:
            where.append("i.id IN (SELECT item_id FROM tags WHERE tag = ?)")
            params.append(tag)
        if status:
            sts = STATUS_GROUPS.get(status, tuple(s.strip() for s in status.split(",")))
            where.append(f"i.verify_status IN ({','.join('?' * len(sts))})")
            params += list(sts)
        if year_from:
            where.append("i.year >= ?")
            params.append(int(year_from))
        if year_to:
            where.append("i.year <= ?")
            params.append(int(year_to))
        if item_type:
            where.append("i.type = ?")
            params.append(item_type)
        if special == "unfiled":
            where.append("i.id NOT IN (SELECT item_id FROM collection_items)")
        elif special == "recent":
            sort, order = "date_added", "desc"
        elif special == "duplicates":
            dup_ids = [it["id"] for g in self.duplicate_groups() for it in g]
            where.append(f"i.id IN ({','.join(str(int(x)) for x in dup_ids) or 'NULL'})")
            if sort == "relevance":
                sort, order = "title", "asc"
        sort_cols = {"title": "i.title_norm", "creator": "i.first_family", "year": "i.year", "date_added": "i.date_added",
                     "container": "i.container", "citekey": "i.citekey", "status": "i.verify_status",
                     "date_modified": "i.date_modified"}
        if sort == "relevance":
            order_sql = f"{rank_expr} ASC" if query and self.has_fts else "i.date_added DESC"
        else:
            col = sort_cols.get(sort, "i.date_added")
            order_sql = f"{col} {'ASC' if order == 'asc' else 'DESC'}, i.id DESC"
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self.conn.execute(f"SELECT COUNT(*) FROM items i{join}{wsql}", params).fetchone()[0]
            rows = self.conn.execute(f"SELECT i.* FROM items i{join}{wsql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
                                     params + [int(limit), int(offset)]).fetchall()
            return [self._row(r) for r in rows], total

    # ------------------------------------------------------------ collections
    def _collection_path(self, cid: int) -> str:
        parts = []
        seen = set()
        while cid and cid not in seen:
            seen.add(cid)
            r = self.conn.execute("SELECT name, parent_id FROM collections WHERE id = ?", (cid,)).fetchone()
            if not r:
                break
            parts.append(r["name"])
            cid = r["parent_id"]
        return "/".join(reversed(parts))

    def _descendants(self, cid: int) -> list[int]:
        out, frontier = [], [cid]
        while frontier:
            nxt = []
            for c in frontier:
                nxt += [r[0] for r in self.conn.execute("SELECT id FROM collections WHERE parent_id = ?", (c,))]
            out += nxt
            frontier = nxt
        return out

    def _collection_id(self, ref: Any, create: bool = False, parent_id: int | None = None) -> int | None:
        with self._lock:
            if isinstance(ref, int):
                r = self.conn.execute("SELECT id FROM collections WHERE id = ?", (ref,)).fetchone()
                return r[0] if r else None
            name = str(ref).strip()
            if name.isdigit():  # a collection may be named "2024"; names win over ids for strings
                r = self.conn.execute("SELECT id FROM collections WHERE name = ? ORDER BY parent_id IS NOT NULL, id", (name,)).fetchone()
                if r:
                    return r[0]
                if not create:
                    r = self.conn.execute("SELECT id FROM collections WHERE id = ?", (int(name),)).fetchone()
                    return r[0] if r else None
            if not name:
                return None
            # "Parent/Child" paths
            if "/" in name and parent_id is None:
                cid = None
                for part in [p for p in name.split("/") if p.strip()]:
                    cid = self._collection_id(part, create=create, parent_id=cid if cid else 0)
                    if cid is None:
                        break
                if cid is not None:
                    return cid
            if parent_id is None:
                r = self.conn.execute("SELECT id FROM collections WHERE name = ? COLLATE NOCASE ORDER BY parent_id IS NOT NULL, id",
                                      (name,)).fetchone()
            elif parent_id == 0:
                r = self.conn.execute("SELECT id FROM collections WHERE name = ? COLLATE NOCASE AND parent_id IS NULL", (name,)).fetchone()
            else:
                r = self.conn.execute("SELECT id FROM collections WHERE name = ? COLLATE NOCASE AND parent_id = ?",
                                      (name, parent_id)).fetchone()
            if r:
                return r[0]
            if not create:
                return None
            cur = self.conn.execute("INSERT INTO collections(name, parent_id, date_added) VALUES (?, ?, ?)",
                                    (name, parent_id or None, now_iso()))
            return cur.lastrowid

    def collection(self, ref: Any) -> dict | None:
        cid = self._collection_id(ref)
        if cid is None:
            return None
        r = self.q1("SELECT * FROM collections WHERE id = ?", (cid,))
        return {"id": r["id"], "name": r["name"], "parent_id": r["parent_id"], "path": self._collection_path(r["id"])}

    def collections_tree(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM collections ORDER BY name COLLATE NOCASE").fetchall()
            counts = dict(self.conn.execute(
                "SELECT ci.collection_id, COUNT(*) FROM collection_items ci JOIN items i ON i.id = ci.item_id WHERE i.deleted = 0 GROUP BY ci.collection_id").fetchall())
        nodes = {r["id"]: {"id": r["id"], "name": r["name"], "parent_id": r["parent_id"], "count": counts.get(r["id"], 0),
                           "children": []} for r in rows}
        roots = []
        for n in nodes.values():
            if n["parent_id"] and n["parent_id"] in nodes:
                nodes[n["parent_id"]]["children"].append(n)
            else:
                roots.append(n)
        return roots

    def create_collection(self, name: str, parent: Any = None, zotero: tuple[int, str] | None = None) -> dict:
        with self.tx():
            pid = self._collection_id(parent) if parent not in (None, "") else None
            if parent not in (None, "") and pid is None:
                raise NotFound(f"No collection '{parent}'")
            existing = self.conn.execute(
                "SELECT id FROM collections WHERE name = ? COLLATE NOCASE AND parent_id IS ?", (name.strip(), pid)).fetchone()
            if existing:
                cid = existing[0]
            else:
                cid = self.conn.execute("INSERT INTO collections(name, parent_id, zotero_library, zotero_key, date_added) VALUES (?,?,?,?,?)",
                                        (name.strip(), pid, zotero[0] if zotero else None, zotero[1] if zotero else None,
                                         now_iso())).lastrowid
        return self.collection(cid)

    def rename_collection(self, ref: Any, new_name: str) -> dict:
        with self.tx():
            cid = self._collection_id(ref)
            if cid is None:
                raise NotFound(f"No collection '{ref}'")
            self.conn.execute("UPDATE collections SET name = ? WHERE id = ?", (new_name.strip(), cid))
        return self.collection(cid)

    def move_collection(self, ref: Any, new_parent: Any = None) -> dict:
        with self.tx():
            cid = self._collection_id(ref)
            if cid is None:
                raise NotFound(f"No collection '{ref}'")
            pid = self._collection_id(new_parent) if new_parent not in (None, "") else None
            if pid == cid or (pid and pid in self._descendants(cid)):
                raise ValueError("Cannot move a collection inside itself")
            self.conn.execute("UPDATE collections SET parent_id = ? WHERE id = ?", (pid, cid))
        return self.collection(cid)

    def delete_collection(self, ref: Any) -> None:
        """Deletes the collection (and sub-collections). Items are kept."""
        with self.tx():
            cid = self._collection_id(ref)
            if cid is None:
                raise NotFound(f"No collection '{ref}'")
            for c in [cid] + self._descendants(cid):
                z = self.conn.execute("SELECT zotero_library, zotero_key FROM collections WHERE id = ?", (c,)).fetchone()
                if z:
                    self._tombstone("collection", z[0], z[1])
            self.conn.execute("DELETE FROM collections WHERE id = ?", (cid,))

    def add_to_collection(self, coll: Any, refs: Iterable[Any], create: bool = True) -> int:
        with self.tx():
            cid = self._collection_id(coll, create=create)
            if cid is None:
                raise NotFound(f"No collection '{coll}'")
            n = 0
            for ref in refs:
                iid = self.resolve_id(ref)
                if iid is None:
                    raise NotFound(f"No item '{ref}'")
                n += self.conn.execute("INSERT OR IGNORE INTO collection_items(collection_id, item_id) VALUES (?, ?)", (cid, iid)).rowcount
            return n

    def remove_from_collection(self, coll: Any, refs: Iterable[Any]) -> int:
        with self.tx():
            cid = self._collection_id(coll)
            if cid is None:
                raise NotFound(f"No collection '{coll}'")
            n = 0
            for ref in refs:
                iid = self.resolve_id(ref)
                if iid is not None:
                    n += self.conn.execute("DELETE FROM collection_items WHERE collection_id = ? AND item_id = ?", (cid, iid)).rowcount
            return n

    def collection_item_ids(self, coll: Any, recursive: bool = True) -> list[int]:
        cid = self._collection_id(coll)
        if cid is None:
            raise NotFound(f"No collection '{coll}'")
        ids = [cid] + (self._descendants(cid) if recursive else [])
        return [r[0] for r in self.q(
            f"SELECT DISTINCT ci.item_id FROM collection_items ci JOIN items i ON i.id = ci.item_id WHERE i.deleted = 0 AND ci.collection_id IN ({','.join('?' * len(ids))})",
            ids)]

    # ------------------------------------------------------------------ tags
    def set_tags(self, ref: Any, tags: Iterable[str], actor: str = "user") -> list[str]:
        with self.tx():
            iid = self.resolve_id(ref)
            if iid is None:
                raise NotFound(f"No item '{ref}'")
            self.conn.execute("DELETE FROM tags WHERE item_id = ?", (iid,))
            for t in tags:
                if t and t.strip():
                    self.conn.execute("INSERT OR IGNORE INTO tags(item_id, tag) VALUES (?, ?)", (iid, t.strip()))
            self._reindex(iid)
            return [r[0] for r in self.conn.execute("SELECT tag FROM tags WHERE item_id = ? ORDER BY tag", (iid,))]

    def add_tags(self, refs: Iterable[Any], tags: Iterable[str]) -> None:
        with self.tx():
            for ref in refs:
                iid = self.resolve_id(ref)
                if iid is None:
                    raise NotFound(f"No item '{ref}'")
                for t in tags:
                    if t and t.strip():
                        self.conn.execute("INSERT OR IGNORE INTO tags(item_id, tag) VALUES (?, ?)", (iid, t.strip()))
                self._reindex(iid)

    def all_tags(self) -> list[dict]:
        return [{"tag": r[0], "count": r[1]} for r in self.q(
            "SELECT t.tag, COUNT(*) FROM tags t JOIN items i ON i.id = t.item_id WHERE i.deleted = 0 GROUP BY t.tag ORDER BY t.tag COLLATE NOCASE")]

    # ----------------------------------------------------------- notes, files
    def add_note(self, ref: Any, html: str, zotero: tuple[int, str] | None = None, date_added: str | None = None) -> int:
        with self.tx():
            iid = self.resolve_id(ref)
            if iid is None:
                raise NotFound(f"No item '{ref}'")
            ts = now_iso()
            nid = self.conn.execute(
                "INSERT INTO notes(item_id, html, date_added, date_modified, zotero_library, zotero_key) VALUES (?,?,?,?,?,?)",
                (iid, html, date_added or ts, ts, zotero[0] if zotero else None, zotero[1] if zotero else None)).lastrowid
            self._reindex(iid)
            return nid

    def update_note(self, note_id: int, html: str) -> None:
        with self.tx():
            r = self.conn.execute("SELECT item_id FROM notes WHERE id = ?", (note_id,)).fetchone()
            if not r:
                raise NotFound(f"No note {note_id}")
            self.conn.execute("UPDATE notes SET html = ?, date_modified = ? WHERE id = ?", (html, now_iso(), note_id))
            self._reindex(r[0])

    def delete_note(self, note_id: int) -> None:
        with self.tx():
            r = self.conn.execute("SELECT item_id, html, zotero_library, zotero_key FROM notes WHERE id = ?", (note_id,)).fetchone()
            if r:
                self._tombstone("note", r[2], r[3])
                self.log(r[0], "delete-note", {"html": r[1]})
                self.conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
                self._reindex(r[0])

    def add_attachment(self, ref: Any, *, title: str = "", path: str | None = None, url: str | None = None,
                       content_type: str | None = None, link_mode: str = "linked_file", fulltext_path: str | None = None,
                       zotero: tuple[int, str] | None = None) -> int:
        with self.tx():
            iid = self.resolve_id(ref)
            if iid is None:
                raise NotFound(f"No item '{ref}'")
            return self.conn.execute(
                """INSERT INTO attachments(item_id, title, path, url, content_type, link_mode, fulltext_path, zotero_library, zotero_key, date_added)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (iid, title, path, url, content_type, link_mode, fulltext_path, zotero[0] if zotero else None,
                 zotero[1] if zotero else None, now_iso())).lastrowid

    def attachment(self, att_id: int) -> dict | None:
        r = self.q1("SELECT * FROM attachments WHERE id = ?", (att_id,))
        return dict(r) if r else None

    # ----------------------------------------------------------- verification
    def _set_verification(self, iid: int, status: str, details: dict | None, suggestions: list | None) -> None:
        self.conn.execute("UPDATE items SET verify_status = ?, verify_checked = ?, verify_details = ?, suggestions = ? WHERE id = ?",
                          (status, now_iso(), json.dumps(details or {}, ensure_ascii=False),
                           json.dumps(suggestions or [], ensure_ascii=False), iid))

    def set_accepted(self, ref: Any, accepted: dict, actor: str = "user") -> None:
        """Registry values the user has seen and chosen not to adopt (verification won't flag them again)."""
        with self.tx():
            iid = self.resolve_id(ref, include_deleted=True)
            if iid is None:
                raise NotFound(f"No item '{ref}'")
            self.conn.execute("UPDATE items SET accepted = ? WHERE id = ?", (json.dumps(accepted, ensure_ascii=False), iid))
            self.log(iid, "keep-library-values", {"fields": sorted(accepted)}, actor)

    def set_verification(self, ref: Any, status: str, details: dict | None = None, suggestions: list | None = None) -> None:
        with self.tx():
            iid = self.resolve_id(ref, include_deleted=True)
            if iid is None:
                raise NotFound(f"No item '{ref}'")
            self._set_verification(iid, status, details, suggestions)

    # ---------------------------------------------------------------- misc
    def history(self, ref: Any, limit: int = 50) -> list[dict]:
        iid = self.resolve_id(ref, include_deleted=True)
        if iid is None:
            return []
        out = []
        for r in self.q("SELECT ts, actor, action, detail FROM history WHERE item_id = ? ORDER BY id DESC LIMIT ?", (iid, limit)):
            d = dict(r)
            try:
                d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            except (TypeError, ValueError):
                pass
            out.append(d)
        return out

    def stats(self) -> dict:
        by_status = dict(self.q("SELECT verify_status, COUNT(*) FROM items WHERE deleted = 0 GROUP BY verify_status"))
        by_type = dict(self.q("SELECT type, COUNT(*) FROM items WHERE deleted = 0 GROUP BY type ORDER BY 2 DESC"))
        return {
            "items": self.count(),
            "trash": self.q1("SELECT COUNT(*) FROM items WHERE deleted = 1")[0],
            "collections": self.q1("SELECT COUNT(*) FROM collections")[0],
            "with_doi": self.q1("SELECT COUNT(*) FROM items WHERE deleted = 0 AND doi IS NOT NULL")[0],
            "pdfs": self.q1("SELECT COUNT(DISTINCT item_id) FROM attachments WHERE content_type = 'application/pdf'")[0],
            "by_status": by_status,
            "by_type": by_type,
            "last_zotero_sync": self.get_meta("last_zotero_sync"),
            "library_path": str(self.path),
        }

    def backup_if_due(self) -> Path | None:
        """Keep one SQLite snapshot per day (the newest `backup_keep` days)."""
        bdir = self.cfg.backups_dir
        bdir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        target = bdir / f"library-{today}.sqlite"
        if target.exists() or not self.path.exists():
            return None
        with self._lock:
            dst = sqlite3.connect(str(target))
            try:
                self.conn.backup(dst)
            finally:
                dst.close()
        keep = int(self.cfg.get("backup_keep", 14) or 14)
        olds = sorted(bdir.glob("library-*.sqlite"))
        for old in olds[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
        return target

    def auto_export(self, force: bool = False) -> list[Path]:
        """Rewrite library.bib / library.json if the library changed since the last export."""
        from .bibtex import to_bibtex
        from .exporters import to_csljson

        ver = str(self.data_version)
        if not force and self.get_meta("exported_version") == ver:
            return []
        targets = self.cfg.get("auto_export") or {}
        written = []
        items = list(self.iter_items())
        for kind, rel in targets.items():
            path = self.cfg.resolve(rel)
            if not path:
                continue
            if kind == "bibtex":
                text = to_bibtex(items, flavor="bibtex", ascii_only=bool(self.cfg.get("bibtex_ascii")),
                                 protected=self.cfg.get("protected_words") or [])
            elif kind == "biblatex":
                text = to_bibtex(items, flavor="biblatex", ascii_only=bool(self.cfg.get("bibtex_ascii")),
                                 protected=self.cfg.get("protected_words") or [])
            elif kind == "csljson":
                text = to_csljson(items)
            else:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
            written.append(path)
        self.set_meta("exported_version", ver)
        return written


def _containers_conflict(a: str | None, b: str | None) -> bool:
    """True when both works name a journal/meeting/book and the names clearly differ (abbreviations allowed)."""
    if not a or not b:
        return False
    fa, fb = norm_title(a), norm_title(b)
    if not fa or not fb or fa == fb or title_similarity(fa, fb) >= 0.8:
        return False
    wa, wb = fa.split(), fb.split()
    short, long_ = (wa, wb) if len(fa) <= len(fb) else (wb, wa)
    i = 0
    for w in long_:  # "geophys res lett" vs "geophysical research letters"
        if i < len(short) and w.startswith(short[i]):
            i += 1
    return i < len(short)


def _arxiv_of(csl: dict) -> str | None:
    for cand in (csl.get("DOI"), csl.get("number"), csl.get("URL"), csl.get("arxiv")):
        if cand and ("arxiv" in str(cand).lower() or str(cand).startswith("10.48550")):
            a = normalize_arxiv(str(cand))
            if a:
                return a
    note = csl.get("note") or ""
    m = re.search(r"arxiv:\s*([^\s\]]+)", note, re.I)
    if m:
        return normalize_arxiv(m.group(1))
    return None


def _fts_query(q: str) -> str:
    """Turn 'author:phillips salt 2023' into an FTS5 MATCH expression (prefix AND search)."""
    parts = []
    for m in re.finditer(r'(\w+):"([^"]+)"|(\w+):(\S+)|"([^"]+)"|(\S+)', q):
        field, val = None, None
        if m.group(1):
            field, val = m.group(1).lower(), m.group(2)
        elif m.group(3) and m.group(3).lower() in FTS_COLUMNS:
            field, val = m.group(3).lower(), m.group(4)
        elif m.group(5):
            val = m.group(5)
        else:
            val = m.group(0)
        toks = [t for t in re.split(r"[^\w]+", val, flags=re.UNICODE) if t]
        if not toks:
            continue
        col = FTS_COLUMNS.get(field) if field else None
        phrase = " ".join(toks)
        expr = f'"{phrase}"' if (m.group(2) or m.group(5)) else " AND ".join(f'"{t}"*' for t in toks)
        parts.append(f"{col} : ({expr})" if col else f"({expr})")
    return " AND ".join(parts)
