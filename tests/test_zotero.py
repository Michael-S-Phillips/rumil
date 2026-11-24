from rumil.zotero import import_zotero


def test_import_and_resync(svc, minizotero):
    rep = import_zotero(svc.lib)
    assert rep["added"] == 4, rep
    assert len(rep["merged_identical"]) == 1  # AAAA2222 has the same DOI as AAAA1111
    assert rep["notes"] == 1 and rep["attachments"] == 1 and rep["pdfs_missing"] == 0
    salt = svc.lib.get("phillipsSaltConstructsPaleoLake2023a")
    assert salt is not None, "Zotero citationKey must be preserved"
    c = salt["csl"]
    assert c["issued"] == {"date-parts": [[2023, 1, 5]]}
    assert "note" not in c, "RIS residue 'Type: Journal Article' should be dropped"
    assert sorted(salt["tags"]) == ["astrobiology", "salts"]
    assert {x["path"] for x in salt["collections"]} == {"hollows_SCUBED", "hollows_SCUBED/subtopic"}
    assert salt["attachments"][0]["fulltext_path"].endswith(".zotero-ft-cache")
    assert "biosignatures" in salt["notes"][0]["html"]
    mercury = svc.lib.search("author:domingue")[0][0]
    assert "URL" not in mercury["csl"], "Web of Science placeholder URLs are dropped"
    chapter = svc.lib.search("tharsis superplume")[0][0]["csl"]
    assert chapter["type"] == "chapter" and chapter["container-title"] == "Superplumes: Beyond Plate Tectonics"
    assert chapter["editor"] == [{"family": "Yuen", "given": "David A."}]
    pre = svc.lib.search("pattern alternations")[0][0]["csl"]
    assert pre["type"] == "article" and pre["number"] == "arXiv:2208.03533" and pre["publisher"] == "arXiv"

    # Re-running changes nothing.
    rep2 = import_zotero(svc.lib)
    assert rep2["added"] == 0 and rep2["updated"] == 0 and rep2["unchanged"] == 5

    # An edit in Zotero flows through.
    mz = minizotero
    iid = mz.db.execute("SELECT itemID FROM items WHERE key = 'CCCC1111'").fetchone()[0]
    mz.set_fields(iid, {"title": "Tharsis superplume and the geological evolution of early Mars (revised)"}, modified="2024-01-01 00:00:00")
    rep3 = import_zotero(svc.lib)
    assert rep3["updated"] == 1
    assert svc.lib.search("revised")[1] == 1
    key = svc.lib.search("tharsis")[0][0]["citekey"]

    # Field-level three-way merge: Rumil's edit to one field survives Zotero's edit to another.
    svc.lib.update_item(key, patch={"volume": "1"}, reason="local fix")
    mz.set_fields(iid, {"pages": "507-523"}, modified="2025-01-01 00:00:00")
    rep4 = import_zotero(svc.lib)
    assert rep4["updated"] == 1 and not rep4["conflicts"]
    c = svc.lib.get(key)["csl"]
    assert c["volume"] == "1" and c["page"] == "507-523"

    # Same field edited on both sides: Rumil wins, every time (not just the first sync after the edit).
    svc.lib.update_item(key, patch={"page": "500-530"}, reason="local fix")
    mz.set_fields(iid, {"pages": "507-524"}, modified="2025-02-01 00:00:00")
    rep5 = import_zotero(svc.lib)
    assert rep5["conflicts"] and rep5["conflicts"][0]["fields"] == ["page"]
    assert svc.lib.get(key)["csl"]["page"] == "500-530"
    mz.set_fields(iid, {"pages": "507-525", "publisher": "Springer Netherlands"}, modified="2025-03-01 00:00:00")
    import_zotero(svc.lib)
    c = svc.lib.get(key)["csl"]
    assert c["page"] == "500-530" and c["publisher"] == "Springer Netherlands"

    # Tags/collections removed, collections renamed, and notes edited or deleted in Rumil stay that way.
    svc.lib.set_tags("phillipsSaltConstructsPaleoLake2023a", ["salts"])
    coll = svc.lib.collection("hollows_SCUBED")
    svc.lib.rename_collection(coll["id"], "Hollows proposal")
    salt = svc.lib.get("phillipsSaltConstructsPaleoLake2023a")
    svc.lib.update_note(salt["notes"][0]["id"], "<p>My edited note</p>")
    import_zotero(svc.lib)
    salt = svc.lib.get("phillipsSaltConstructsPaleoLake2023a")
    assert salt["tags"] == ["salts"]
    assert svc.lib.collection("Hollows proposal") and not svc.lib.collection("hollows_SCUBED")
    assert salt["notes"][0]["html"] == "<p>My edited note</p>"
    svc.lib.delete_note(salt["notes"][0]["id"])
    import_zotero(svc.lib)
    assert svc.lib.get("phillipsSaltConstructsPaleoLake2023a")["notes"] == []

    # Purged in Rumil: not re-imported.
    svc.lib.trash(key)
    svc.lib.empty_trash()
    rep7 = import_zotero(svc.lib)
    assert rep7["added"] == 0 and rep7["skipped_deleted_in_rumil"] == 1


def test_zotero_key_collision_is_reported(svc, minizotero):
    svc.lib.add_item({"type": "book", "title": "Some other book", "author": [{"family": "Other"}], "issued": "1999"},
                     source="manual", citekey="phillipsSaltConstructsPaleoLake2023a")
    rep = import_zotero(svc.lib)
    assert rep["key_conflicts"] and rep["key_conflicts"][0]["zotero_citekey"] == "phillipsSaltConstructsPaleoLake2023a"


def test_fulltext_from_zotero_cache(svc, minizotero):
    from rumil.fulltext import item_fulltext
    import_zotero(svc.lib)
    r = item_fulltext(svc.lib, "phillipsSaltConstructsPaleoLake2023a")
    assert r["available"] and "salt constructs" in r["text"]
    q = item_fulltext(svc.lib, "phillipsSaltConstructsPaleoLake2023a", query="salt constructs")
    assert q["matches"]
