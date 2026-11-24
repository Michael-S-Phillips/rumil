import pytest

from rumil.resolvers import NotFoundError


def test_resolve_crossref(svc):
    r = svc.resolver.resolve("https://doi.org/10.3390/rs15020314")
    c = r.csl
    assert r.source == "crossref"
    assert c["type"] == "article-journal"
    assert c["title"].startswith("Salt Constructs in Paleo-Lake Basins")
    assert len(c["author"]) == 7 and c["author"][5]["family"] == "Rivera-Valentín"
    assert c["container-title"] == "Remote Sensing" and c["volume"] == "15" and c["issue"] == "2" and c["page"] == "314"
    assert c["issued"] == {"date-parts": [[2023, 1, 5]]}
    assert "<jats" not in c["abstract"]


def test_unknown_doi_is_not_found(svc):
    with pytest.raises(NotFoundError):
        svc.resolver.resolve("10.9999/does.not.exist.12345")


def test_retraction_detected(svc):
    r = svc.resolver.resolve("10.1177/1758835920922055")
    assert r.registry_record["retracted"] is True
    assert r.registry_record["updates"][0]["notice_doi"] == "10.1177/17588359231172420"


def test_datacite_and_isbn_and_arxiv(svc):
    d = svc.resolver.resolve("10.5281/zenodo.5789166")
    assert d.source == "datacite" and d.csl["type"] == "dataset" and d.csl["author"][0]["family"] == "Dunn"
    b = svc.resolver.resolve("ISBN 978-0-521-85371-2")
    assert b.csl["type"] == "book"
    assert [a["family"] for a in b.csl["author"]] == ["de Pater", "Lissauer"]  # Google Books fills the second author
    a = svc.resolver.resolve("arXiv:2208.03533")
    assert a.csl["type"] == "article" and a.csl["DOI"] == "10.48550/arXiv.2208.03533"
    assert a.csl["title"] == "Pattern alternations induced by nonlocal interactions"
    assert a.registry_record["published_version_dois"] == ["10.9999/test.published-version"]


def test_webpage_with_doi_meta_resolves_via_registry(svc):
    r = svc.resolver.resolve("https://www.mdpi.com/2072-4292/15/2/314")
    assert r.source == "crossref" and r.authoritative
    assert r.pdf_url == "https://www.mdpi.com/2072-4292/15/2/314/pdf"


def test_webpage_without_doi(svc):
    r = svc.resolver.resolve("https://water.lsbu.ac.uk/water/water_vibrational_spectrum.html")
    assert r.source == "web" and not r.authoritative
    assert r.csl["type"] == "webpage" and r.csl["author"][0]["family"] == "Chaplin"
    assert r.csl["issued"] == {"date-parts": [[2019, 5, 2]]}


def test_add_by_doi_is_verified_and_dedupes(svc):
    r = svc.add("10.1130/G50341.1", collection="Hellas", tags=["feldspar"])
    assert r["status"] == "added"
    it = r["item"]
    assert it["citekey"] == "phillipsExtensiveAncientFeldspathic2022"
    assert it["status"] == "verified"
    again = svc.add("https://doi.org/10.1130/g50341.1")
    assert again["status"] == "exists" and again["item"]["citekey"] == it["citekey"]
    assert svc.lib.count() == 1


def test_verify_detects_mismatch(svc):
    item = svc.lib.add_item({"type": "article-journal", "title": "A completely different paper about Venus",
                             "author": [{"family": "Smith", "given": "J."}], "issued": "2010", "DOI": "10.1130/G50341.1"},
                            source="manual")
    out = svc.verify([item["citekey"]])["results"][0]
    assert out["status"] == "mismatch"
    assert any(s["kind"] == "replace_metadata" for s in out["suggestions"])


def test_verify_invalid_doi_and_retracted(svc):
    bad = svc.lib.add_item({"type": "article-journal", "title": "Invented paper", "author": [{"family": "Nobody"}],
                            "issued": "2021", "DOI": "10.9999/does.not.exist.12345"}, source="manual")
    ret = svc.lib.add_item({"type": "article-journal", "title": "Myc is a prognostic biomarker and potential therapeutic target in osteosarcoma",
                            "author": [{"family": "Feng", "given": "Wenlong"}], "issued": "2020", "DOI": "10.1177/1758835920922055"},
                           source="manual")
    res = {r["citekey"]: r for r in svc.verify([bad["citekey"], ret["citekey"]])["results"]}
    assert res[bad["citekey"]]["status"] == "doi_invalid"
    assert res[ret["citekey"]]["status"] == "retracted"
    assert "RETRACTED" in res[ret["citekey"]]["summary"]


def test_missing_doi_found_and_safe_fix(svc):
    it = svc.lib.add_item({"type": "article-journal",
                           "title": "Mercury's global color mosaic: An update from MESSENGER's orbital observations",
                           "author": [{"family": "Domingue", "given": "D. L."}], "issued": "2015", "container-title": "Icarus",
                           "volume": "257", "page": "477-488", "URL": "<Go to ISI>://WOS:000358101300037"}, source="zotero")
    out = svc.verify([it["citekey"]])["results"][0]
    assert out["status"] == "doi_found"
    kinds = {s["kind"]: s for s in out["suggestions"]}
    assert kinds["add_doi"]["proposed"] == "10.1016/j.icarus.2014.11.027" and kinds["add_doi"]["safe"]
    assert kinds["clean_field"]["field"] == "URL" and kinds["clean_field"]["safe"]
    fixed = svc.apply_fixes(it["citekey"])
    assert fixed["status"] == "verified"
    rec = svc.lib.get(it["citekey"])
    assert rec["csl"]["DOI"] == "10.1016/j.icarus.2014.11.027" and "URL" not in rec["csl"]
    assert rec["csl"]["author"][0]["family"] == "Domingue"  # existing values never overwritten by safe fixes
    hist = svc.lib.history(it["citekey"])
    assert any(h["action"] == "edit" for h in hist)


def test_unverifiable_and_web(svc):
    it = svc.lib.add_item({"type": "paper-conference", "title": "Investigation of Noachian crustal structure",
                           "author": [{"family": "Dapremont"}], "issued": "2017", "container-title": "LPSC"}, source="zotero")
    assert svc.verify([it["citekey"]])["results"][0]["status"] == "unverified"
    web = svc.add("https://water.lsbu.ac.uk/water/water_vibrational_spectrum.html")
    assert web["item"]["status"] == "url_ok"
    dead = svc.lib.add_item({"type": "webpage", "title": "Gone", "URL": "https://dead.example.org/x"}, source="manual")
    assert svc.verify([dead["citekey"]])["results"][0]["status"] == "url_dead"


def test_preprint_published_version_suggested(svc):
    r = svc.add("arXiv:2208.03533")
    it = svc.lib.get(r["item"]["citekey"])
    assert it["verify_status"] == "verified"
    assert any(s["kind"] == "published_version" and s["proposed"] == "10.9999/test.published-version"
               for s in it["suggestions"])


def test_manual_with_evidence(svc):
    r = svc.add_manual({"type": "webpage", "title": "Water absorption spectrum", "author": [{"family": "Chaplin", "given": "Martin"}]},
                       evidence_url="https://water.lsbu.ac.uk/water/water_vibrational_spectrum.html")
    assert r["item"]["status"] == "verified_no_doi"
    r2 = svc.add_manual({"type": "webpage", "title": "Something not on that page at all", "author": [{"family": "Nobody"}]},
                        evidence_url="https://water.lsbu.ac.uk/water/other.html")
    assert r2["item"]["status"] == "unverified"


def test_find_reference(svc):
    svc.add("10.1130/G50341.1")
    out = svc.find(title="Mercury's global color mosaic: An update from MESSENGER's orbital observations", author="Domingue", year=2015)
    top = out["candidates"][0]
    assert top["DOI"] == "10.1016/j.icarus.2014.11.027" and top["confidence"] == "high"
    lib_hit = svc.find("feldspathic Hellas", registries=False)["in_library"]
    assert lib_hit and lib_hit[0]["citekey"] == "phillipsExtensiveAncientFeldspathic2022"
