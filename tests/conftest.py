"""Shared test fixtures. Registry responses are replayed from tests/fixtures (captured from the live APIs,
trimmed) so tests run offline. Anything not in the table answers 404, like an unknown DOI would."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from rumil.config import Config
from rumil.http import Http, Response

FIX = Path(__file__).parent / "fixtures"


def _load(name: str) -> bytes:
    return (FIX / name).read_bytes()


CROSSREF = {
    "10.3390/rs15020314": "crossref_rs15020314.json",
    "10.1130/g50341.1": "crossref_g50341.json",
    "10.1177/1758835920922055": "crossref_retracted.json",
    "10.1016/j.icarus.2014.11.027": "crossref_icarus_mercury.json",
    "10.1111/j.1945-5100.1998.tb01695.x": "crossref_cintala.json",
    # Shaped like Crossref's real record for this DOI (October 2026): no 'page', and the AGU manuscript number in
    # 'article-number'. Title and authors trimmed for the test.
    "10.1029/2006je002868": "crossref_agu_2006je002868.json",
    # Christensen et al. (2005) and its erratum (Nature 436, 882), shaped like Crossref's records; trimmed author list.
    "10.1038/nature03639": "crossref_nature03639.json",
    "10.1038/nature04075": "crossref_nature04075.json",
    # A SICI-style GSA DOI with '<', '>' and ';' (shaped like Crossref's record, October 2026).
    "10.1130/0091-7613(2001)029<0987:hsesat>2.0.co;2": "crossref_gsa_cleland2001.json",
}
HANDLES_OK = set(CROSSREF) | {"10.5281/zenodo.5789166", "10.48550/arxiv.2208.03533"}
# Fields Crossref accepts in `select` (from its own validation error); anything else is rejected with HTTP 400.
from rumil.resolvers import CROSSREF_SELECTABLE  # noqa: E402
ADS_BY_DOI = {"10.1029/2006je002868": {"bibcode": "2007JGRE..11210007C", "page": ["E10007"], "doi": ["10.1029/2006JE002868"],
                                       "abstract": "We model near-infrared reflectance spectra of lunar highland soils."}}



class FakeTransport:
    def __init__(self):
        self.calls: list[str] = []
        self.christensen_search = "crossref_search_christensen.json"  # tests may switch to the _linked variant

    def __call__(self, method: str, url: str, headers: dict, timeout: float) -> Response:
        self.calls.append(url)
        u = urlparse(url)
        q = parse_qs(u.query)
        path = unquote(u.path)
        js = {"content-type": "application/json"}
        if u.hostname == "api.crossref.org":
            if path.startswith("/works/"):
                doi = path[len("/works/"):].lower()
                if doi in CROSSREF:
                    return Response(200, url, js, _load(CROSSREF[doi]))
                return Response(404, url, {"content-type": "text/plain"}, b"Resource not found.")
            if path == "/works":
                bad = [f for f in ",".join(q.get("select", [])).split(",") if f and f not in CROSSREF_SELECTABLE]
                if bad:
                    body = {"status": "failed", "message-type": "validation-failure",
                            "message": [{"type": "select-not-available", "value": bad[0]}]}
                    return Response(400, url, js, json.dumps(body).encode())
                bib = " ".join(q.get("query.bibliographic", [""])).lower()
                name = "crossref_search_mercury.json" if "mercury" in bib else "crossref_search_empty.json"
                if "historical science" in bib:
                    name = "crossref_search_cleland.json"
                if "magmatic evolution" in bib:
                    name = self.christensen_search
                return Response(200, url, js, _load(name))
        if u.hostname == "api.datacite.org":
            doi = path[len("/dois/"):].lower()
            if doi == "10.5281/zenodo.5789166":
                return Response(200, url, js, _load("datacite_zenodo.json"))
            if doi.startswith("10.48550/arxiv.2208.03533"):
                body = {"data": {"attributes": {"doi": "10.48550/arXiv.2208.03533", "titles": [{"title": "Pattern alternations induced by nonlocal interactions"}],
                                                "creators": [{"familyName": "Pal", "givenName": "Swadesh"}, {"familyName": "Melnik", "givenName": "Roderick"},
                                                             {"familyName": "Banerjee", "givenName": "Malay"}],
                                                "publisher": "arXiv", "publicationYear": 2022, "types": {"citeproc": "article"}}}}
                return Response(200, url, js, json.dumps(body).encode())
            return Response(404, url, js, b'{"errors":[{"status":"404"}]}')
        if u.hostname == "doi.org":
            if path.startswith("/api/handles/"):
                doi = path[len("/api/handles/"):].lower()
                return Response(200 if doi in HANDLES_OK else 404, url, js,
                                _load("handle_ok.json" if doi in HANDLES_OK else "handle_404.json"))
            return Response(404, url, {}, b"")
        if u.hostname == "export.arxiv.org":
            if "2208.03533" in u.query:
                return Response(200, url, {"content-type": "application/atom+xml"}, _load("arxiv_2208.03533.xml"))
            return Response(200, url, {"content-type": "application/atom+xml"},
                            b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>')
        if u.hostname == "openlibrary.org":
            if "9780521853712" in u.query:
                return Response(200, url, js, _load("openlibrary_depater.json"))
            return Response(200, url, js, b"{}")
        if u.hostname == "www.googleapis.com":
            if "9780521853712" in u.query:
                return Response(200, url, js, _load("googlebooks_depater.json"))
            return Response(200, url, js, b'{"totalItems":0}')
        if u.hostname == "www.mdpi.com":
            return Response(200, url, {"content-type": "text/html; charset=utf-8"}, _load("page_with_meta.html"))
        if u.hostname == "water.lsbu.ac.uk":
            return Response(200, url, {"content-type": "text/html; charset=utf-8"}, _load("page_no_doi.html"))
        if u.hostname in ("www.hou.usra.edu", "www.lpi.usra.edu"):
            if path == "/meetings/lpsc2022/pdf/1234.pdf":
                return Response(200, url, {"content-type": "application/pdf"}, b"%PDF-1.4 fake LPSC abstract")
            return Response(404, url, {}, b"")
        if u.hostname == "api.adsabs.harvard.edu":
            qq = " ".join(q.get("q", [""]))
            docs = [d for k, d in ADS_BY_DOI.items() if f'doi:"{k}"' in qq.lower()]
            return Response(200, url, js, json.dumps({"response": {"numFound": len(docs), "docs": docs}}).encode())
        if u.hostname == "dead.example.org":
            return Response(404, url, {}, b"")
        return Response(404, url, {}, b"")


@pytest.fixture
def fake():
    return FakeTransport()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMIL_HOME", str(tmp_path / "Rumil"))
    c = Config(tmp_path / "Rumil")
    c.set("zotero_dir", str(tmp_path / "Zotero"))
    c.save()
    return c


@pytest.fixture
def svc(cfg, fake):
    from rumil.service import Rumil
    s = Rumil(cfg, http=Http(transport=fake))
    yield s
    s.flush()
    s.lib.close()


# ------------------------------------------------------------ mini Zotero DB
ZOTERO_SCHEMA = """
CREATE TABLE libraries (libraryID INTEGER PRIMARY KEY, type TEXT NOT NULL, editable INT, filesEditable INT, version INT, storageVersion INT, lastSync INT, archived INT);
CREATE TABLE groups (groupID INTEGER PRIMARY KEY, libraryID INT NOT NULL UNIQUE, name TEXT NOT NULL, description TEXT, version INT);
CREATE TABLE itemTypesCombined (itemTypeID INT NOT NULL, typeName TEXT NOT NULL, display INT DEFAULT 1, custom INT NOT NULL);
CREATE TABLE fieldsCombined (fieldID INT NOT NULL, fieldName TEXT NOT NULL, label TEXT, fieldFormatID INT, custom INT NOT NULL);
CREATE TABLE creatorTypes (creatorTypeID INTEGER PRIMARY KEY, creatorType TEXT);
CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INT NOT NULL, dateAdded TEXT, dateModified TEXT, clientDateModified TEXT, libraryID INT NOT NULL, key TEXT NOT NULL, version INT DEFAULT 0, synced INT DEFAULT 0);
CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value UNIQUE);
CREATE TABLE itemData (itemID INT, fieldID INT, valueID INT, PRIMARY KEY (itemID, fieldID));
CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT, fieldMode INT);
CREATE TABLE itemCreators (itemID INT, creatorID INT, creatorTypeID INT, orderIndex INT, PRIMARY KEY (itemID, orderIndex));
CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE itemTags (itemID INT, tagID INT, type INT, PRIMARY KEY (itemID, tagID));
CREATE TABLE collections (collectionID INTEGER PRIMARY KEY, collectionName TEXT NOT NULL, parentCollectionID INT, clientDateModified TEXT, libraryID INT NOT NULL, key TEXT NOT NULL, version INT DEFAULT 0, synced INT DEFAULT 0);
CREATE TABLE collectionItems (collectionID INT, itemID INT, orderIndex INT DEFAULT 0, PRIMARY KEY (collectionID, itemID));
CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY, parentItemID INT, linkMode INT, contentType TEXT, charsetID INT, path TEXT, syncState INT, storageModTime INT, storageHash TEXT, lastProcessedModificationTime INT);
CREATE TABLE itemNotes (itemID INTEGER PRIMARY KEY, parentItemID INT, note TEXT, title TEXT);
CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY, dateDeleted DEFAULT CURRENT_TIMESTAMP NOT NULL);
CREATE TABLE settings (setting TEXT, key TEXT, value, PRIMARY KEY (setting, key));
"""


class MiniZotero:
    TYPES = {"journalArticle": 22, "bookSection": 7, "attachment": 3, "note": 28, "preprint": 30}
    FIELDS = ["title", "date", "publicationTitle", "volume", "issue", "pages", "DOI", "url", "extra", "bookTitle",
              "publisher", "place", "citationKey", "abstractNote", "accessDate", "repository", "archiveID"]
    CREATOR_TYPES = {"author": 8, "editor": 10, "bookAuthor": 12}

    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "storage").mkdir(exist_ok=True)
        self.db = sqlite3.connect(folder / "zotero.sqlite")
        self.db.executescript(ZOTERO_SCHEMA)
        self.db.execute("INSERT INTO libraries VALUES (1,'user',1,1,0,0,0,0)")
        self.db.execute("INSERT INTO settings VALUES ('client','lastVersion','9.0.6')")
        for name, tid in self.TYPES.items():
            self.db.execute("INSERT INTO itemTypesCombined VALUES (?,?,1,0)", (tid, name))
        for i, f in enumerate(self.FIELDS, 1):
            self.db.execute("INSERT INTO fieldsCombined VALUES (?,?,?,NULL,0)", (i, f, f))
        for name, cid in self.CREATOR_TYPES.items():
            self.db.execute("INSERT INTO creatorTypes VALUES (?,?)", (cid, name))
        self.db.commit()
        self._id = 0

    def _value(self, v):
        r = self.db.execute("SELECT valueID FROM itemDataValues WHERE value = ?", (v,)).fetchone()
        if r:
            return r[0]
        return self.db.execute("INSERT INTO itemDataValues(value) VALUES (?)", (v,)).lastrowid

    def add_item(self, key, itype, fields, creators=(), tags=(), collections=(), lib=1, added="2023-04-05 14:17:46",
                 modified="2023-04-05 14:17:46"):
        cur = self.db.execute("INSERT INTO items(itemTypeID, dateAdded, dateModified, libraryID, key) VALUES (?,?,?,?,?)",
                              (self.TYPES[itype], added, modified, lib, key))
        iid = cur.lastrowid
        self.set_fields(iid, fields)
        for i, (last, first, ctype) in enumerate(creators):
            cid = self.db.execute("INSERT INTO creators(firstName, lastName, fieldMode) VALUES (?,?,?)",
                                  (first, last, 0 if first else 1)).lastrowid
            self.db.execute("INSERT INTO itemCreators VALUES (?,?,?,?)", (iid, cid, self.CREATOR_TYPES[ctype], i))
        for t in tags:
            r = self.db.execute("SELECT tagID FROM tags WHERE name = ?", (t,)).fetchone()
            tid = r[0] if r else self.db.execute("INSERT INTO tags(name) VALUES (?)", (t,)).lastrowid
            self.db.execute("INSERT INTO itemTags VALUES (?,?,0)", (iid, tid))
        for c in collections:
            self.db.execute("INSERT OR IGNORE INTO collectionItems VALUES (?,?,0)", (c, iid))
        self.db.commit()
        return iid

    def set_fields(self, iid, fields, modified=None):
        fid = {f: i for i, f in enumerate(self.FIELDS, 1)}
        for k, v in fields.items():
            self.db.execute("INSERT OR REPLACE INTO itemData VALUES (?,?,?)", (iid, fid[k], self._value(v)))
        if modified:
            self.db.execute("UPDATE items SET dateModified = ? WHERE itemID = ?", (modified, iid))
        self.db.commit()

    def add_collection(self, cid, name, key, parent=None, lib=1):
        self.db.execute("INSERT INTO collections(collectionID, collectionName, parentCollectionID, libraryID, key) VALUES (?,?,?,?,?)",
                        (cid, name, parent, lib, key))
        self.db.commit()

    def add_pdf(self, parent_iid, key, filename, fulltext="Full text of the paper about salt constructs."):
        iid = self.db.execute("INSERT INTO items(itemTypeID, dateAdded, dateModified, libraryID, key) VALUES (3,'2023-04-05 14:17:46','2023-04-05 14:17:46',1,?)",
                              (key,)).lastrowid
        self.db.execute("INSERT INTO itemAttachments(itemID, parentItemID, linkMode, contentType, path) VALUES (?,?,1,'application/pdf',?)",
                        (iid, parent_iid, f"storage:{filename}"))
        self.set_fields(iid, {"title": "Full Text PDF"})
        d = self.folder / "storage" / key
        d.mkdir(parents=True, exist_ok=True)
        (d / filename).write_bytes(b"%PDF-1.4 fake")
        (d / ".zotero-ft-cache").write_text(fulltext)
        self.db.commit()

    def add_note(self, parent_iid, key, html):
        iid = self.db.execute("INSERT INTO items(itemTypeID, dateAdded, dateModified, libraryID, key) VALUES (28,'2023-04-06 10:00:00','2023-04-06 10:00:00',1,?)",
                              (key,)).lastrowid
        self.db.execute("INSERT INTO itemNotes VALUES (?,?,?,?)", (iid, parent_iid, html, ""))
        self.db.commit()

    def close(self):
        self.db.close()


@pytest.fixture
def minizotero(cfg):
    z = MiniZotero(Path(cfg.get("zotero_dir")))
    z.add_collection(1, "hollows_SCUBED", "COLL0001")
    z.add_collection(2, "subtopic", "COLL0002", parent=1)
    a = z.add_item("AAAA1111", "journalArticle", {
        "title": "Salt Constructs in Paleo-Lake Basins as High-Priority Astrobiology Targets", "date": "2023-01-05 2023-01-05",
        "publicationTitle": "Remote Sensing", "volume": "15", "issue": "2", "pages": "314", "DOI": "10.3390/rs15020314",
        "url": "https://www.mdpi.com/2072-4292/15/2/314", "citationKey": "phillipsSaltConstructsPaleoLake2023a",
        "extra": "Type: Journal Article"},
        creators=[("Phillips", "Michael S.", "author"), ("McInenly", "Michael", "author")], tags=["salts", "astrobiology"],
        collections=[1])
    z.add_item("AAAA2222", "journalArticle", {  # Zotero-side duplicate (same DOI)
        "title": "Salt constructs in paleo-lake basins as high-priority astrobiology targets", "date": "2023-00-00 2023",
        "DOI": "10.3390/rs15020314", "publicationTitle": "Remote Sensing"},
        creators=[("Phillips", "M. S.", "author")], collections=[2])
    z.add_item("BBBB1111", "journalArticle", {
        "title": "Mercury's global color mosaic: An update from MESSENGER's orbital observations", "date": "2015-00-00 2015",
        "publicationTitle": "Icarus", "volume": "257", "pages": "477-488", "url": "<Go to ISI>://WOS:000358101300037",
        "extra": "Type: Journal Article"},
        creators=[("Domingue", "D. L.", "author"), ("Murchie", "S. L.", "author"), ("Denevi", "B. W.", "author")], collections=[1])
    z.add_item("CCCC1111", "bookSection", {
        "title": "Tharsis superplume and the geological evolution of early Mars", "date": "2007-00-00 2007",
        "bookTitle": "Superplumes: Beyond Plate Tectonics", "publisher": "Springer", "place": "Dordrecht", "pages": "507-522"},
        creators=[("Baker", "Victor R.", "author"), ("Yuen", "David A.", "editor")])
    z.add_item("DDDD1111", "preprint", {
        "title": "Pattern alternations induced by nonlocal interactions", "date": "2022-08-06 2022-08-06",
        "repository": "arXiv", "archiveID": "arXiv:2208.03533", "DOI": "10.48550/arXiv.2208.03533",
        "extra": "arXiv:2208.03533 [math]"},
        creators=[("Pal", "Swadesh", "author"), ("Melnik", "Roderick", "author"), ("Banerjee", "Malay", "author")])
    z.add_pdf(a, "PDF00001", "Phillips_2023.pdf")
    z.add_note(a, "NOTE0001", "<p>Key result: gypsum crusts preserve <b>biosignatures</b>.</p>")
    yield z
    z.close()
