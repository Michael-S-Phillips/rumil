"""Import (and re-sync) a local Zotero library straight from zotero.sqlite.

Reads a copy of the database, so Zotero can stay open. Brings over items,
creators, collections (including group libraries), tags, child notes, PDF and
other attachments (linked in place, not copied), and Zotero/Better BibTeX
citation keys. Re-running the import picks up items added or edited in Zotero
since the last sync; edits made in Rumil are never overwritten.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable

from .csl import CSL_TYPES, clean_item, make_date, parse_date
from .db import Library, now_iso
from .ids import doi_key, is_junk_url, normalize_arxiv, normalize_doi

ZOTERO_TYPES = {
    "journalArticle": "article-journal", "book": "book", "bookSection": "chapter", "conferencePaper": "paper-conference",
    "thesis": "thesis", "report": "report", "webpage": "webpage", "preprint": "article", "dataset": "dataset",
    "computerProgram": "software", "manuscript": "manuscript", "presentation": "speech",
    "magazineArticle": "article-magazine", "newspaperArticle": "article-newspaper", "patent": "patent", "map": "map",
    "document": "document", "blogPost": "post-weblog", "forumPost": "post", "encyclopediaArticle": "entry-encyclopedia",
    "dictionaryEntry": "entry-dictionary", "standard": "standard", "letter": "personal_communication",
    "email": "personal_communication", "instantMessage": "personal_communication", "interview": "interview",
    "film": "motion_picture", "artwork": "graphic", "audioRecording": "song", "videoRecording": "motion_picture",
    "tvBroadcast": "broadcast", "radioBroadcast": "broadcast", "podcast": "song", "bill": "bill", "case": "legal_case",
    "statute": "legislation", "hearing": "hearing",
}

FIELD_MAP = {
    "title": "title", "abstractNote": "abstract", "publicationTitle": "container-title", "bookTitle": "container-title",
    "proceedingsTitle": "container-title", "websiteTitle": "container-title", "blogTitle": "container-title",
    "forumTitle": "container-title", "encyclopediaTitle": "container-title", "dictionaryTitle": "container-title",
    "programTitle": "container-title", "seriesTitle": "collection-title", "series": "collection-title",
    "seriesNumber": "collection-number", "volume": "volume", "issue": "issue", "pages": "page",
    "numPages": "number-of-pages", "numberOfVolumes": "number-of-volumes", "edition": "edition",
    "place": "publisher-place", "publisher": "publisher", "institution": "publisher", "university": "publisher",
    "company": "publisher", "label": "publisher", "distributor": "publisher", "studio": "publisher",
    "network": "publisher", "repository": "publisher", "archive": "archive", "archiveLocation": "archive_location",
    "DOI": "DOI", "ISBN": "ISBN", "ISSN": "ISSN", "url": "URL", "language": "language", "shortTitle": "title-short",
    "journalAbbreviation": "container-title-short", "callNumber": "call-number", "libraryCatalog": "source",
    "reportNumber": "number", "reportType": "genre", "thesisType": "genre", "websiteType": "genre",
    "presentationType": "genre", "manuscriptType": "genre", "mapType": "genre", "letterType": "genre", "genre": "genre",
    "conferenceName": "event-title", "meetingName": "event-title", "versionNumber": "version", "number": "number",
    "patentNumber": "number", "billNumber": "number", "docketNumber": "number", "section": "section", "scale": "scale",
    "medium": "medium", "artworkMedium": "medium", "audioRecordingFormat": "medium", "videoRecordingFormat": "medium",
    "runningTime": "dimensions", "court": "authority", "legislativeBody": "authority", "PMID": "PMID", "PMCID": "PMCID",
    "archiveID": "number", "status": "status", "filingDate": "submitted", "issueDate": "issued",
    "dateDecided": "issued", "dateEnacted": "issued",
}

CREATOR_MAP = {
    "author": "author", "editor": "editor", "bookAuthor": "container-author", "seriesEditor": "collection-editor",
    "translator": "translator", "reviewedAuthor": "reviewed-author", "contributor": "contributor",
    "director": "director", "interviewer": "interviewer", "recipient": "recipient", "composer": "composer",
    "producer": "producer", "scriptwriter": "script-writer", "guest": "guest", "performer": "performer",
    "castMember": "performer", "commenter": "commenter", "cosponsor": "contributor", "counsel": "contributor",
    "attorneyAgent": "contributor",
}  # anything else (programmer, presenter, inventor, cartographer, artist, interviewee, podcaster, sponsor, wordsBy) -> author

_RIS_RESIDUE = re.compile(r"^\s*Type:\s*(Journal Article|Conference Paper|Conference Proceedings?|Book|Book Section|"
                          r"Electronic Book|Web Page|Report|Thesis|Generic|Dataset|Computer Program|Map|"
                          r"Electronic Article|Edited Book|Abstract)\s*$", re.I)
_CSL_VAR_LINE = re.compile(r"^\s*([a-z][a-z\-_]+|DOI|ISBN|ISSN|PMID|PMCID|URL)\s*:\s*(.+?)\s*$")


def _zdate(v: str | None) -> dict | None:
    """Zotero stores dates as 'YYYY-MM-DD original text' with 00 for unknown parts."""
    if not v:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?: (.*))?$", v)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y == 0:
            return parse_date(m.group(4)) if m.group(4) else None
        return make_date(y, mo or None, d or None)
    return parse_date(v)


def _zts(v: str | None) -> str | None:
    """'2023-04-05 14:17:46' (UTC) -> ISO 8601."""
    if not v:
        return None
    return v.replace(" ", "T") + ("+00:00" if "+" not in v and "Z" not in v else "")


def _parse_extra(extra: str, csl: dict) -> tuple[dict, str | None, int, str | None]:
    """Pull CSL variables and citation keys out of Zotero's Extra field.

    Returns (fields, citekey, residue_lines_removed, remaining_note)."""
    fields: dict[str, Any] = {}
    citekey = None
    keep, removed = [], 0
    for line in (extra or "").splitlines():
        if _RIS_RESIDUE.match(line):
            removed += 1
            continue
        m = re.match(r"^\s*Citation Key\s*:\s*(\S+)\s*$", line, re.I)
        if m:
            citekey = m.group(1)
            continue
        m = re.match(r"^\s*arXiv\s*:\s*(\S+)", line, re.I)
        if m:
            aid = normalize_arxiv(m.group(1))
            if aid:
                fields.setdefault("number", f"arXiv:{aid}")
            keep.append(line)
            continue
        m = _CSL_VAR_LINE.match(line)
        if m:
            var, val = m.group(1), m.group(2)
            var_l = var.lower() if var not in ("DOI", "ISBN", "ISSN", "PMID", "PMCID", "URL") else var
            if var in ("DOI",) and not csl.get("DOI"):
                d = normalize_doi(val)
                if d:
                    fields["DOI"] = d
                    continue
            if var_l in ("original-date", "event-date", "submitted", "available-date") and var_l not in csl:
                fields[var_l] = parse_date(val[:10] if re.match(r"\d{4}-\d\d-\d\d", val) else val)
                continue
            if var_l in ("container-title", "event-place", "event-title", "genre", "medium", "status",
                         "publisher-place", "number", "PMID", "PMCID", "version", "language") and var_l not in csl:
                fields[var_l] = val
                continue
            if var_l == "type" and val in CSL_TYPES:
                fields["type"] = val
                continue
        keep.append(line)
    note = "\n".join(keep).strip() or None
    return fields, citekey, removed, note


class ZoteroReader:
    def __init__(self, zotero_dir: str | os.PathLike):
        self.dir = Path(os.path.expanduser(str(zotero_dir)))
        self.db_file = self.dir / "zotero.sqlite"
        if not self.db_file.exists():
            raise FileNotFoundError(f"No Zotero library (zotero.sqlite) in {self.dir}")
        self._tmpdir = tempfile.mkdtemp(prefix="rumil-zotero-")
        copy = Path(self._tmpdir) / "zotero.sqlite"
        shutil.copy2(self.db_file, copy)
        self.conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        ok = self.conn.execute("PRAGMA quick_check").fetchone()[0]
        if ok != "ok":  # caught Zotero mid-write; fall back to its own backup
            self.conn.close()
            bak = self.dir / "zotero.sqlite.bak"
            if not bak.exists():
                raise RuntimeError("zotero.sqlite copy is inconsistent and no zotero.sqlite.bak exists; close Zotero and retry")
            shutil.copy2(bak, copy)
            self.conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master")}
        self.types_tbl = "itemTypesCombined" if "itemTypesCombined" in tables else "itemTypes"
        self.fields_tbl = "fieldsCombined" if "fieldsCombined" in tables else "fields"

    def close(self) -> None:
        self.conn.close()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def q(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def libraries(self) -> dict[int, dict]:
        libs = {}
        for r in self.q("SELECT libraryID, type FROM libraries"):
            libs[r["libraryID"]] = {"id": r["libraryID"], "type": r["type"], "name": "My Library" if r["type"] == "user" else None}
        try:
            for r in self.q("SELECT libraryID, name FROM groups"):
                if r["libraryID"] in libs:
                    libs[r["libraryID"]]["name"] = r["name"]
        except sqlite3.OperationalError:
            pass
        return {k: v for k, v in libs.items() if v["type"] in ("user", "group")}

    def version(self) -> str | None:
        try:
            r = self.conn.execute("SELECT value FROM settings WHERE setting = 'client' AND key = 'lastVersion'").fetchone()
            return r[0] if r else None
        except sqlite3.OperationalError:
            return None

    def read(self, library_ids: list[int]) -> dict:
        lib_list = ",".join(str(int(x)) for x in library_ids) or "NULL"
        deleted = {r[0] for r in self.q("SELECT itemID FROM deletedItems")}
        rows = self.q(f"""SELECT i.itemID, i.key, i.libraryID, i.dateAdded, i.dateModified, t.typeName
                          FROM items i JOIN {self.types_tbl} t ON t.itemTypeID = i.itemTypeID
                          WHERE i.libraryID IN ({lib_list})""")
        items, attachments, notes = {}, [], []
        for r in rows:
            if r["itemID"] in deleted:
                continue
            tn = r["typeName"]
            if tn == "attachment":
                attachments.append(r)
            elif tn == "note":
                notes.append(r)
            elif tn == "annotation":
                continue
            else:
                items[r["itemID"]] = {"row": r, "fields": {}, "creators": [], "tags": [], "collections": []}
        for r in self.q(f"""SELECT d.itemID, f.fieldName, v.value FROM itemData d
                            JOIN {self.fields_tbl} f ON f.fieldID = d.fieldID
                            JOIN itemDataValues v ON v.valueID = d.valueID"""):
            if r["itemID"] in items:
                items[r["itemID"]]["fields"][r["fieldName"]] = r["value"]
        for r in self.q("""SELECT ic.itemID, c.firstName, c.lastName, c.fieldMode, ct.creatorType, ic.orderIndex
                           FROM itemCreators ic JOIN creators c ON c.creatorID = ic.creatorID
                           JOIN creatorTypes ct ON ct.creatorTypeID = ic.creatorTypeID ORDER BY ic.itemID, ic.orderIndex"""):
            if r["itemID"] in items:
                items[r["itemID"]]["creators"].append(dict(r))
        for r in self.q("SELECT it.itemID, t.name FROM itemTags it JOIN tags t ON t.tagID = it.tagID"):
            if r["itemID"] in items:
                items[r["itemID"]]["tags"].append(r["name"])
        for r in self.q("SELECT collectionID, itemID FROM collectionItems"):
            if r["itemID"] in items:
                items[r["itemID"]]["collections"].append(r["collectionID"])
        collections = [dict(r) for r in self.q(
            f"SELECT collectionID, collectionName, parentCollectionID, libraryID, key FROM collections WHERE libraryID IN ({lib_list})")]
        att_out = []
        att_ids = [a["itemID"] for a in attachments]
        att_info = {r["itemID"]: r for r in self.q("SELECT itemID, parentItemID, linkMode, contentType, path FROM itemAttachments")}
        att_fields: dict[int, dict] = {}
        if att_ids:
            for r in self.q(f"""SELECT d.itemID, f.fieldName, v.value FROM itemData d
                                JOIN {self.fields_tbl} f ON f.fieldID = d.fieldID
                                JOIN itemDataValues v ON v.valueID = d.valueID
                                WHERE f.fieldName IN ('title', 'url')"""):
                att_fields.setdefault(r["itemID"], {})[r["fieldName"]] = r["value"]
        standalone_att = 0
        for a in attachments:
            info = att_info.get(a["itemID"])
            if not info:
                continue
            if info["parentItemID"] is None or info["parentItemID"] not in items:
                standalone_att += 1
                continue
            att_out.append({"key": a["key"], "libraryID": a["libraryID"], "parent": info["parentItemID"],
                            "linkMode": info["linkMode"], "contentType": info["contentType"], "path": info["path"],
                            "title": att_fields.get(a["itemID"], {}).get("title"),
                            "url": att_fields.get(a["itemID"], {}).get("url")})
        note_rows = {r["itemID"]: r for r in self.q("SELECT itemID, parentItemID, note FROM itemNotes")}
        notes_out, standalone_notes = [], 0
        for n in notes:
            nr = note_rows.get(n["itemID"])
            if not nr:
                continue
            if nr["parentItemID"] is None or nr["parentItemID"] not in items:
                standalone_notes += 1
                continue
            notes_out.append({"key": n["key"], "libraryID": n["libraryID"], "parent": nr["parentItemID"],
                              "html": nr["note"] or "", "dateAdded": n["dateAdded"]})
        return {"items": items, "collections": collections, "attachments": att_out, "notes": notes_out,
                "standalone_attachments": standalone_att, "standalone_notes": standalone_notes}

    def attachment_paths(self, att: dict) -> tuple[str | None, str | None, str]:
        """Return (file path, fulltext cache path, link mode name)."""
        mode = {0: "imported_file", 1: "imported_url", 2: "linked_file", 3: "linked_url", 4: "embedded_image"}.get(att["linkMode"], "other")
        path = att.get("path") or ""
        storage = self.dir / "storage" / att["key"]
        ft = storage / ".zotero-ft-cache"
        ft_path = str(ft) if ft.exists() else None
        if mode in ("imported_file", "imported_url") and path.startswith("storage:"):
            return str(storage / path[len("storage:"):]), ft_path, mode
        if mode == "linked_file":
            if path.startswith("attachments:"):
                return path, ft_path, mode  # relative to Zotero's linked-attachment base directory
            return path or None, ft_path, mode
        return None, ft_path, mode


def item_to_csl(z: dict) -> tuple[dict, str | None, int, list[str]]:
    """Convert one Zotero item (fields + creators) to CSL-JSON. Returns (csl, citekey, residue_removed, cleanup_notes)."""
    tn = z["row"]["typeName"]
    f = z["fields"]
    csl: dict[str, Any] = {"type": ZOTERO_TYPES.get(tn, "document")}
    cleanup = []
    for zf, val in f.items():
        if zf in ("date", "accessDate", "extra", "citationKey", "rights", "programmingLanguage", "dateAdded", "dateModified"):
            continue
        var = FIELD_MAP.get(zf)
        if not var or not val:
            continue
        if var == "issued":
            d = _zdate(val)
            if d:
                csl["issued"] = d
            continue
        if var == "submitted":
            csl["submitted"] = _zdate(val)
            continue
        if var == "number" and zf == "archiveID":
            aid = normalize_arxiv(val)
            csl["number"] = f"arXiv:{aid}" if aid else val
            continue
        if var in csl and var == "publisher":
            continue
        csl[var] = val
    d = _zdate(f.get("date"))
    if d:
        csl["issued"] = d
    if f.get("accessDate"):
        acc = parse_date(f["accessDate"][:10])
        if acc:
            csl["accessed"] = acc
    if csl.get("URL") and is_junk_url(csl["URL"]):
        cleanup.append(f"dropped placeholder URL {csl['URL'][:40]}")
        csl.pop("URL")
    names: dict[str, list] = {}
    for c in z["creators"]:
        role = CREATOR_MAP.get(c["creatorType"], "author")
        if c["fieldMode"] == 1 or not c["firstName"]:
            n = {"literal": c["lastName"]} if c["fieldMode"] == 1 else {"family": c["lastName"]}
        else:
            n = {"family": c["lastName"], "given": c["firstName"]}
        names.setdefault(role, []).append(n)
    # Book sections: Zotero 'bookAuthor' is the book's author; keep chapter authors as author.
    csl.update(names)
    citekey = f.get("citationKey")
    removed = 0
    if f.get("extra"):
        extra_fields, ek, removed, kept = _parse_extra(f["extra"], csl)
        citekey = citekey or ek
        csl.update({k: v for k, v in extra_fields.items() if v})
        if kept:
            csl["note"] = kept
    if removed:
        cleanup.append("removed RIS import residue from Extra")
    return clean_item(csl), citekey, removed, cleanup


def _merge3(base: dict, local: dict, new: dict) -> tuple[dict, list[str]]:
    """Three-way merge of CSL records: take Zotero's change unless the field was also edited in Rumil."""
    merged, conflicts = {}, []
    for k in set(base) | set(local) | set(new):
        b, l, n = base.get(k), local.get(k), new.get(k)
        if l == b:
            v = n
        elif n == b or l == n:
            v = l
        else:
            v = l
            conflicts.append(k)
        if v not in (None, "", []):
            merged[k] = v
    return merged, sorted(conflicts)


def _merge_sets(base: set, local: set, new: set) -> tuple[set, set]:
    """Apply Zotero's additions/removals (new vs base) to the local set. Returns (to_add, to_remove)."""
    added, removed = new - base, base - new
    return added - local, removed & local


def import_zotero(lib: Library, zotero_dir: str | os.PathLike | None = None, *, libraries: list[str] | None = None,
                  merge_identical: bool = True, dry_run: bool = False,
                  progress: Callable[[str, int, int], None] | None = None) -> dict:
    """Import or re-sync from Zotero. Safe to run repeatedly.

    Re-syncs are three-way merges against the last imported Zotero version: a field, tag, collection
    membership, note, or collection name changed in Rumil is never overwritten; Zotero's changes to anything
    left alone in Rumil come through. Things deleted in Rumil are not brought back."""
    zdir = Path(os.path.expanduser(str(zotero_dir or lib.cfg.zotero_dir)))
    reader = ZoteroReader(zdir)
    report: dict[str, Any] = {"zotero_dir": str(zdir), "zotero_version": reader.version(), "added": 0, "updated": 0,
                              "unchanged": 0, "linked_existing": 0, "merged_identical": [], "conflicts": [],
                              "key_conflicts": [], "zotero_citekey_changed": [], "collections_created": 0, "notes": 0,
                              "notes_updated": 0, "attachments": 0, "pdfs_missing": 0, "cleaned": 0,
                              "skipped_deleted_in_rumil": 0, "skipped": {}, "errors": []}
    try:
        libs = reader.libraries()
        if libraries:
            wanted = {l.lower() for l in libraries}
            libs = {k: v for k, v in libs.items() if (v["name"] or "").lower() in wanted or str(k) in wanted}
        data = reader.read(list(libs))
        report["skipped"] = {"standalone_notes": data["standalone_notes"],
                             "standalone_attachments": data["standalone_attachments"]}
        report["libraries"] = [v["name"] for v in libs.values()]
        if dry_run:
            report["would_import_items"] = len(data["items"])
            report["collections"] = len(data["collections"])
            return report
        ts = now_iso()
        conn = lib.conn
        with lib.tx():
            tomb = {(r[0], r[1], r[2]) for r in conn.execute("SELECT kind, zotero_library, zotero_key FROM zotero_tombstones")}

            # ---- collections
            coll_map: dict[int, int] = {}
            group_root: dict[int, int] = {}
            for lid, info in libs.items():
                if info["type"] == "group":
                    root = conn.execute("SELECT id FROM collections WHERE zotero_library = ? AND zotero_key = '__group__'",
                                        (lid,)).fetchone()
                    if root:
                        group_root[lid] = root[0]
                    elif ("collection", lid, "__group__") not in tomb:
                        group_root[lid] = conn.execute(
                            "INSERT INTO collections(name, parent_id, zotero_library, zotero_key, zotero_name, date_added) VALUES (?, NULL, ?, '__group__', ?, ?)",
                            (f"{info['name']} (group)", lid, f"{info['name']} (group)", ts)).lastrowid
                        report["collections_created"] += 1
            existing = {(r["zotero_library"], r["zotero_key"]): dict(r) for r in conn.execute(
                "SELECT id, name, parent_id, zotero_library, zotero_key, zotero_name, zotero_parent FROM collections WHERE zotero_key IS NOT NULL")}
            by_zid = {c["collectionID"]: c for c in data["collections"]}

            def ensure(c):
                if c["collectionID"] in coll_map:
                    return coll_map[c["collectionID"]]
                if ("collection", c["libraryID"], c["key"]) in tomb:
                    coll_map[c["collectionID"]] = None
                    return None
                parent = None
                if c["parentCollectionID"] and c["parentCollectionID"] in by_zid:
                    parent = ensure(by_zid[c["parentCollectionID"]])
                elif c["libraryID"] in group_root:
                    parent = group_root[c["libraryID"]]
                k = (c["libraryID"], c["key"])
                if k in existing:
                    e = existing[k]
                    cid = e["id"]
                    # Rename / move only if Rumil hasn't renamed / moved it since the last sync.
                    name = c["collectionName"] if e["name"] == (e["zotero_name"] or e["name"]) else e["name"]
                    # zotero_parent: NULL = never recorded (legacy), 0 = top level, else the parent id at last sync
                    base_parent = e["zotero_parent"]
                    if base_parent is None or (e["parent_id"] or 0) == base_parent:
                        par = parent
                    else:
                        par = e["parent_id"]  # moved in Rumil: keep it there
                    conn.execute("UPDATE collections SET name = ?, parent_id = ?, zotero_name = ?, zotero_parent = ? WHERE id = ?",
                                 (name, par, c["collectionName"], parent or 0, cid))
                else:
                    cid = conn.execute(
                        "INSERT INTO collections(name, parent_id, zotero_library, zotero_key, zotero_name, zotero_parent, date_added) VALUES (?,?,?,?,?,?,?)",
                        (c["collectionName"], parent, c["libraryID"], c["key"], c["collectionName"], parent or 0, ts)).lastrowid
                    report["collections_created"] += 1
                coll_map[c["collectionID"]] = cid
                return cid

            for c in data["collections"]:
                ensure(c)

            # ---- items
            zmap = {(r["zotero_library"], r["zotero_key"]): dict(r) for r in conn.execute("SELECT * FROM zotero_map")}
            item_map: dict[int, int] = {}  # zotero itemID -> rumil id
            total = len(data["items"])
            for n, (zid, z) in enumerate(sorted(data["items"].items(), key=lambda kv: kv[1]["row"]["dateAdded"] or ""), 1):
                if progress and n % 50 == 0:
                    progress("items", n, total)
                r = z["row"]
                lid, zkey = r["libraryID"], r["key"]
                if ("item", lid, zkey) in tomb:
                    report["skipped_deleted_in_rumil"] += 1
                    continue
                try:
                    csl, ckey, removed, cleanup = item_to_csl(z)
                except Exception as e:
                    report["errors"].append({"zotero_key": zkey, "error": f"{type(e).__name__}: {e}"})
                    continue
                report["cleaned"] += 1 if cleanup else 0
                if not csl.get("title") and not csl.get("author"):
                    report["errors"].append({"zotero_key": zkey, "error": "item has no title or creators"})
                    continue
                zcolls = {coll_map.get(zc) for zc in z["collections"]} - {None}
                if lid in group_root and not z["collections"]:
                    zcolls.add(group_root[lid])
                ztags = set(z["tags"])
                m = zmap.get((lid, zkey))
                if m:
                    iid = m["item_id"]
                    item_map[zid] = iid
                    cur = conn.execute("SELECT citekey, csl, local_modified FROM items WHERE id = ?", (iid,)).fetchone()
                    changed_in_zotero = (r["dateModified"] or "") > (m["zotero_modified"] or "")
                    # A newer Rumil may read the same Zotero item better (e.g. a DOI that an older version cut off
                    # at '<'). Merge that in too; fields edited in Rumil are kept as always.
                    reread = bool(m.get("zotero_csl")) and json.loads(m["zotero_csl"]) != csl
                    if changed_in_zotero or reread:
                        local = json.loads(cur["csl"])
                        if m.get("zotero_csl"):
                            merged, conflicts = _merge3(json.loads(m["zotero_csl"]), local, csl)
                        elif cur["local_modified"] and m["synced_at"] and cur["local_modified"] > m["synced_at"]:
                            merged, conflicts = local, ["(edited in Rumil)"]
                        else:
                            merged, conflicts = csl, []
                        if not changed_in_zotero:
                            conflicts = []  # only Rumil's reading changed; Rumil's own edits win quietly
                        if clean_item(merged) != clean_item(local):
                            lib.update_item(iid, csl=merged, actor="zotero-sync", from_sync=True,
                                            reason="Zotero item changed" if changed_in_zotero else "re-read from Zotero")
                            report["updated"] += 1
                        else:
                            report["unchanged"] += 1
                        if conflicts:
                            report["conflicts"].append({"citekey": cur["citekey"], "fields": conflicts, "message":
                                                        "Changed in both Zotero and Rumil; kept the Rumil values for these fields"})
                    else:
                        report["unchanged"] += 1
                    if ckey and m.get("zotero_citekey") and ckey != m["zotero_citekey"]:
                        report["zotero_citekey_changed"].append({"citekey": cur["citekey"], "zotero_was": m["zotero_citekey"],
                                                                 "zotero_now": ckey})
                    # Tags and collection memberships: apply Zotero's additions/removals, keep Rumil's.
                    local_tags = {t[0] for t in conn.execute("SELECT tag FROM tags WHERE item_id = ?", (iid,))}
                    base_tags = set(json.loads(m["zotero_tags"])) if m.get("zotero_tags") else local_tags & ztags
                    add, rem = _merge_sets(base_tags, local_tags, ztags)
                    for t in add:
                        conn.execute("INSERT OR IGNORE INTO tags(item_id, tag) VALUES (?, ?)", (iid, t))
                    for t in rem:
                        conn.execute("DELETE FROM tags WHERE item_id = ? AND tag = ?", (iid, t))
                    local_colls = {c[0] for c in conn.execute("SELECT collection_id FROM collection_items WHERE item_id = ?", (iid,))}
                    base_colls = set(json.loads(m["zotero_colls"])) if m.get("zotero_colls") else local_colls & zcolls
                    add, rem = _merge_sets(base_colls, local_colls, zcolls)
                    for c in add:
                        conn.execute("INSERT OR IGNORE INTO collection_items(collection_id, item_id) VALUES (?, ?)", (c, iid))
                    for c in rem:
                        conn.execute("DELETE FROM collection_items WHERE collection_id = ? AND item_id = ?", (c, iid))
                    conn.execute("""UPDATE zotero_map SET zotero_modified = ?, synced_at = ?, zotero_csl = ?, zotero_tags = ?,
                                    zotero_colls = ?, zotero_citekey = ? WHERE zotero_library = ? AND zotero_key = ?""",
                                 (max(r["dateModified"] or "", m["zotero_modified"] or ""), ts, json.dumps(csl, ensure_ascii=False),
                                  json.dumps(sorted(ztags)), json.dumps(sorted(zcolls)), ckey, lid, zkey))
                    continue

                # ---- not seen before
                iid = None
                if merge_identical and (csl.get("DOI") or str(csl.get("number", "")).startswith("arXiv:")):
                    dk = doi_key(csl.get("DOI"))
                    hit = conn.execute("SELECT id, citekey FROM items WHERE deleted = 0 AND doi = ?", (dk,)).fetchone() if dk else None
                    if hit:
                        iid = hit["id"]
                        mapped_already = conn.execute("SELECT 1 FROM zotero_map WHERE item_id = ?", (iid,)).fetchone()
                        if mapped_already:
                            report["merged_identical"].append({"zotero_key": zkey, "into": hit["citekey"],
                                                               "zotero_citekey": ckey, "reason": "same DOI as another Zotero item"})
                        else:
                            report["linked_existing"] += 1
                        if ckey and ckey.lower() != hit["citekey"].lower():
                            owner = lib.resolve_id(ckey, include_deleted=True)
                            if owner is None:
                                conn.execute("INSERT INTO aliases(alias, item_id) VALUES (?, ?)", (ckey, iid))
                            elif owner != iid:
                                report["key_conflicts"].append({"zotero_citekey": ckey, "now_points_to": lib.get(owner, full=False, include_deleted=True)["citekey"],
                                                                "this_item_is": hit["citekey"]})
                if iid is None:
                    item = lib.add_item(csl, source="zotero", citekey=ckey, actor="zotero-sync", date_added=_zts(r["dateAdded"]))
                    iid = item["id"]
                    report["added"] += 1
                    if ckey and item["citekey"] != ckey:
                        owner = lib.resolve_id(ckey, include_deleted=True)
                        report["key_conflicts"].append({
                            "zotero_citekey": ckey, "imported_as": item["citekey"],
                            "message": f"Zotero key '{ckey}' already names "
                                       f"'{lib.get(owner, full=False, include_deleted=True)['citekey'] if owner else '?'}' in Rumil; "
                                       f"this item was imported as '{item['citekey']}'"})
                for t in ztags:
                    conn.execute("INSERT OR IGNORE INTO tags(item_id, tag) VALUES (?, ?)", (iid, t))
                for c in zcolls:
                    conn.execute("INSERT OR IGNORE INTO collection_items(collection_id, item_id) VALUES (?, ?)", (c, iid))
                conn.execute("""INSERT OR REPLACE INTO zotero_map(zotero_library, zotero_key, item_id, zotero_modified, synced_at,
                                zotero_csl, zotero_tags, zotero_colls, zotero_citekey) VALUES (?,?,?,?,?,?,?,?,?)""",
                             (lid, zkey, iid, r["dateModified"], ts, json.dumps(csl, ensure_ascii=False),
                              json.dumps(sorted(ztags)), json.dumps(sorted(zcolls)), ckey))
                item_map[zid] = iid

            # ---- notes (an edit made in Rumil wins; deleted ones stay deleted)
            have_notes = {(r["zotero_library"], r["zotero_key"]): dict(r) for r in conn.execute(
                "SELECT id, html, zotero_html, zotero_library, zotero_key FROM notes WHERE zotero_key IS NOT NULL")}
            for nrec in data["notes"]:
                iid = item_map.get(nrec["parent"])
                k = (nrec["libraryID"], nrec["key"])
                if not iid or ("note", k[0], k[1]) in tomb:
                    continue
                cur = have_notes.get(k)
                if cur:
                    base = cur["zotero_html"] if cur["zotero_html"] is not None else cur["html"]
                    if nrec["html"] != base:
                        if cur["html"] == base:
                            conn.execute("UPDATE notes SET html = ?, zotero_html = ?, date_modified = ? WHERE id = ?",
                                         (nrec["html"], nrec["html"], ts, cur["id"]))
                            report["notes_updated"] += 1
                        else:
                            conn.execute("UPDATE notes SET zotero_html = ? WHERE id = ?", (nrec["html"], cur["id"]))
                            report["conflicts"].append({"note": nrec["key"], "message": "Note edited in both Zotero and Rumil; kept the Rumil text"})
                else:
                    conn.execute("""INSERT INTO notes(item_id, html, date_added, date_modified, zotero_library, zotero_key, zotero_html)
                                    VALUES (?,?,?,?,?,?,?)""", (iid, nrec["html"], _zts(nrec["dateAdded"]), ts, k[0], k[1], nrec["html"]))
                    report["notes"] += 1

            # ---- attachments (links to files in ~/Zotero/storage; refreshed every sync)
            have_att = {(r[0], r[1]): r[2] for r in conn.execute(
                "SELECT zotero_library, zotero_key, id FROM attachments WHERE zotero_key IS NOT NULL")}
            for a in data["attachments"]:
                iid = item_map.get(a["parent"])
                if not iid:
                    continue
                path, ft, mode = reader.attachment_paths(a)
                if mode == "embedded_image":
                    continue
                if path and mode != "linked_file" and not Path(path).exists():
                    report["pdfs_missing"] += 1
                k = (a["libraryID"], a["key"])
                vals = (iid, a.get("title") or (Path(path).name if path else "Link"), path, a.get("url"), a["contentType"], mode, ft)
                if k in have_att:
                    conn.execute("UPDATE attachments SET item_id=?, title=?, path=?, url=?, content_type=?, link_mode=?, fulltext_path=? WHERE id=?",
                                 vals + (have_att[k],))
                else:
                    conn.execute("""INSERT INTO attachments(item_id, title, path, url, content_type, link_mode, fulltext_path,
                                    zotero_library, zotero_key, date_added) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                                 vals + (k[0], k[1], ts))
                    report["attachments"] += 1

            # ---- items deleted in Zotero since the last sync (reported, never deleted here)
            live = {(z["row"]["libraryID"], z["row"]["key"]) for z in data["items"].values()}
            report["deleted_in_zotero"] = []
            for k in zmap:
                if k[0] in libs and k not in live:
                    row = conn.execute("SELECT i.citekey FROM zotero_map zm JOIN items i ON i.id = zm.item_id "
                                       "WHERE zm.zotero_library = ? AND zm.zotero_key = ?", k).fetchone()
                    if row:
                        report["deleted_in_zotero"].append(row[0])

            for iid in set(item_map.values()):
                lib._reindex(iid)
            lib.set_meta("last_zotero_sync", ts)
        if progress:
            progress("done", total, total)
        report["summary"] = (f"{report['added']} added, {report['updated']} updated, {report['unchanged']} unchanged, "
                             f"{len(report['merged_identical'])} exact duplicates merged, {len(report['conflicts'])} conflicts, "
                             f"{len(report['key_conflicts'])} citekey conflicts; "
                             f"{report['notes']} notes and {report['attachments']} attachments linked")
        return report
    finally:
        reader.close()
