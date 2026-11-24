"""Sixth round: DOIs with '<', '>' and ';' (SICI-style GSA, AGU, Elsevier, and Wiley DOIs) were cut off at '<'."""

import pytest

from rumil.ids import detect, normalize_doi
from rumil.zotero import import_zotero

CLELAND = "10.1130/0091-7613(2001)029<0987:HSESAT>2.0.CO;2"


@pytest.mark.parametrize("raw,expected", [
    (CLELAND, CLELAND),
    ("https://doi.org/10.1130/0091-7613(2001)029%3C0987:HSESAT%3E2.0.CO;2", CLELAND),
    ("https://doi.org/10.1130%2F0091-7613(2001)029%3C0987%3AHSESAT%3E2.0.CO%3B2", CLELAND),
    ("doi:" + CLELAND + ".", CLELAND),
    ("10.1002/(SICI)1097-4636(199709)36:3<347::AID-JBM10>3.0.CO;2-B",
     "10.1002/(SICI)1097-4636(199709)36:3<347::AID-JBM10>3.0.CO;2-B"),
    ("<a href='x'>10.1016/j.icarus.2014.11.027</a>", "10.1016/j.icarus.2014.11.027"),
    ("10.1029/2000JE001289<br>", "10.1029/2000JE001289"),
    ("10.1234/abc<https://example.org>", "10.1234/abc"),
    ("see 10.1016/S0019-1035(03)00123-4).", "10.1016/S0019-1035(03)00123-4"),
])
def test_doi_parsing_keeps_sici_segments(raw, expected):
    assert normalize_doi(raw) == expected


def test_add_by_sici_doi(svc, fake):
    assert detect(CLELAND).value == CLELAND
    out = svc.add(CLELAND)
    it = svc.lib.get(out["item"]["citekey"])
    assert it["csl"]["DOI"] == CLELAND  # the capitals as given, though Crossref lowercases this DOI
    assert it["verify_status"] == "verified"
    # Requests encode the DOI ('<' -> %3C), as Crossref and doi.org expect.
    assert any("%3C0987" in u for u in fake.calls if "api.crossref.org/works/" in u)


def test_doi_cut_off_by_older_versions_is_completed_safely(svc):
    k = svc.lib.add_item({"type": "article-journal", "title": "Historical science, experimental science, and the scientific method",
                          "author": [{"family": "Cleland", "given": "Carol E."}], "issued": "2001",
                          "container-title": "Geology", "volume": "29", "page": "987"}, source="zotero")["citekey"]
    import json
    it = svc.lib.get(k)
    with svc.lib._lock:  # stored the way the older version left it
        svc.lib.conn.execute("UPDATE items SET csl = ? WHERE id = ?",
                             (json.dumps(dict(it["csl"], DOI="10.1130/0091-7613(2001)029")), it["id"]))
        svc.lib.conn.commit()
    r = svc.verify([k])["results"][0]
    assert r["status"] == "doi_invalid" and "cut off" in r["summary"]
    fix = next(s for s in r["suggestions"] if s["kind"] == "replace_doi")
    assert fix["safe"] and fix["proposed"].lower() == CLELAND.lower()
    assert svc.apply_fixes(k, "safe")["status"] == "verified"


def test_zotero_resync_repairs_dois_cut_off_by_an_older_import(svc, cfg, monkeypatch):
    from pathlib import Path
    from conftest import MiniZotero
    z = MiniZotero(Path(cfg.get("zotero_dir")))
    z.add_item("GSA00001", "journalArticle", {
        "title": "Historical science, experimental science, and the scientific method", "date": "2001",
        "publicationTitle": "Geology", "volume": "29", "pages": "987", "DOI": CLELAND},
        creators=[("Cleland", "Carol E.", "author")])
    import rumil.zotero as zmod
    real = zmod.normalize_doi
    monkeypatch.setattr(zmod, "normalize_doi", lambda s: (real(s) or "").split("<")[0] or None)  # the old behavior
    import rumil.csl as cmod
    if hasattr(cmod, "normalize_doi"):
        monkeypatch.setattr(cmod, "normalize_doi", lambda s: (real(s) or "").split("<")[0] or None)
    import_zotero(svc.lib)
    k = svc.lib.search("historical science")[0][0]["citekey"]
    old = svc.lib.get(k)["csl"]["DOI"]
    monkeypatch.undo()
    if old == CLELAND:
        pytest.skip("DOI was not cut off by the simulated old import")
    rep = import_zotero(svc.lib)
    assert rep["updated"] == 1
    assert svc.lib.get(k)["csl"]["DOI"] == CLELAND
