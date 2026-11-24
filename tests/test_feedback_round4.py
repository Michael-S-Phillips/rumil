"""Fourth round, from auditing a review paper's references: Crossref searches were being rejected, AGU manuscript
numbers were printed as citation numbers, the library had lost accents and full given names that the paper's own
.bib had, and loosely typed .bib entries were imported as duplicates."""

import pytest

from rumil import resolvers
from rumil.http import Http, HttpError
from rumil.latex import compare_with_manuscript
from rumil.resolvers import (CROSSREF_SEARCH_FIELDS, CROSSREF_SELECTABLE, Resolution, Resolver, crossref_to_csl,
                             is_agu_manuscript_number)
from rumil.service import Rumil
from rumil.verify import VerifyResult

AGU_DOI = "10.1029/2006JE002868"
AGU_ITEM = {"type": "article-journal", "DOI": AGU_DOI, "issued": "2007", "volume": "112", "issue": "E10",
            "title": "Radiative transfer modeling of lunar highlands spectral classes and relationship to lunar samples",
            "container-title": "Journal of Geophysical Research: Planets",
            "author": [{"family": "Cahill", "given": "Joshua T. S."}, {"family": "Lucey", "given": "Paul G."}]}


def test_crossref_search_asks_only_for_selectable_fields(svc, monkeypatch):
    assert set(CROSSREF_SEARCH_FIELDS) <= CROSSREF_SELECTABLE
    assert "institution" not in CROSSREF_SEARCH_FIELDS
    assert svc.resolver.crossref_search(title="Mercury hollows", author="Blewett") is not None
    # The test transport rejects unknown fields the way Crossref does, so this class of bug can't come back.
    monkeypatch.setattr(resolvers, "CROSSREF_SEARCH_FIELDS", CROSSREF_SEARCH_FIELDS + ("institution",))
    with pytest.raises(HttpError):
        svc.resolver.crossref_search(title="Mercury hollows")


def test_agu_manuscript_numbers():
    assert is_agu_manuscript_number("2006JE002868") and is_agu_manuscript_number("2012GL051711")
    assert not is_agu_manuscript_number("e2019JE006307")  # the e-number is the real citation number
    assert not is_agu_manuscript_number("E10007") and not is_agu_manuscript_number("1234")
    m = {"DOI": AGU_DOI, "type": "journal-article", "title": ["x"], "article-number": "2006JE002868"}
    assert "page" not in crossref_to_csl(m)
    m["article-number"] = "e2019JE006307"
    assert crossref_to_csl(m)["page"] == "e2019JE006307"


def test_manuscript_number_in_library_without_ads_token(svc):
    k = svc.lib.add_item(dict(AGU_ITEM, page="2006JE002868"), source="zotero")["citekey"]
    r = svc.verify([k])["results"][0]
    assert r["status"] == "differs"
    assert any("manuscript number" in i for i in r["issues"])
    fix = next(s for s in r["suggestions"] if s["field"] == "page")
    assert fix["proposed"] is None and not fix["safe"]  # removing it is offered, never done silently


@pytest.fixture
def svc_ads(cfg, fake):
    cfg.set("ads_token", "test-token")
    cfg.save()
    s = Rumil(cfg, http=Http(transport=fake))
    yield s
    s.flush()
    s.lib.close()


def test_manuscript_number_replaced_with_citation_number_from_ads(svc_ads):
    k = svc_ads.lib.add_item(dict(AGU_ITEM, page="2006JE002868"), source="zotero")["citekey"]
    r = svc_ads.verify([k])["results"][0]
    assert r["status"] == "differs"
    fix = next(s for s in r["suggestions"] if s["field"] == "page")
    assert fix["safe"] and fix["proposed"] == "E10007"
    out = svc_ads.apply_fixes(k, "safe")
    assert out["status"] == "verified"
    c = svc_ads.lib.get(k)["csl"]
    assert c["page"] == "E10007" and "near-infrared" in c["abstract"]  # the ADS abstract came along


def test_missing_agu_citation_number_and_abstract_filled_from_ads(svc_ads):
    k = svc_ads.lib.add_item(dict(AGU_ITEM), source="zotero")["citekey"]
    r = svc_ads.verify([k])["results"][0]
    assert r["status"] == "verified"
    fields = {s["field"]: s for s in r["suggestions"] if s.get("safe")}
    assert fields["page"]["proposed"] == "E10007" and "abstract" in fields


def _cmp(local, reg):
    v = Rumil.__new__(Rumil)  # only the verifier's comparison is needed
    from rumil.verify import Verifier
    ver = Verifier.__new__(Verifier)
    ver.resolver = Resolver(Http(transport=lambda *a: None))
    res = VerifyResult(status="unverified", summary="")
    ver._compare(local, Resolution(reg, "crossref", "", {}), res)
    del v
    return res


def test_accents_restored_from_registry():
    local = {"title": "Low sulfide concentration in Mars", "issued": "2023",
             "author": [{"family": "Payre", "given": "V."}, {"family": "Barraud", "given": "Sebastien"}]}
    reg = dict(local, author=[{"family": "Payré", "given": "V."}, {"family": "Barraud", "given": "Sébastien"}])
    res = _cmp(local, reg)
    assert res.status == "differs"
    fix = next(s for s in res.suggestions if s["field"] == "author")
    assert fix["safe"] and [n["family"] for n in fix["proposed"]] == ["Payré", "Barraud"]
    assert fix["proposed"][1]["given"] == "Sébastien"


def test_typographic_hyphens_are_not_accents():
    local = {"title": "A magma ocean", "issued": "2005", "author": [{"family": "Elkins-Tanton", "given": "L. T."}]}
    reg = dict(local, author=[{"family": "Elkins\u2010Tanton", "given": "L. T."}])
    assert _cmp(local, reg).status == "verified"


def test_initials_expanded_from_registry():
    local = {"title": "Diverse volcanism", "issued": "2024",
             "author": [{"family": "Michalski", "given": "J. R."}, {"family": "Viviano", "given": "C. E."}]}
    reg = dict(local, author=[{"family": "Michalski", "given": "Joseph R."}, {"family": "Viviano", "given": "C."}])
    res = _cmp(local, reg)
    assert res.status == "verified"
    fix = next(s for s in res.suggestions if s["field"] == "author")
    assert fix["safe"] and fix["proposed"][0]["given"] == "Joseph R." and fix["proposed"][1]["given"] == "C. E."


def test_compare_with_manuscript_bib():
    lib = {"title": "Sulfur on Mars", "issued": "2006", "volume": "111", "page": "2005JE002499",
           "author": [{"family": "Payre", "given": "V."}, {"family": "Fabre", "given": "C."}]}
    ms = dict(lib, page="E02S05", author=[{"family": "Payré", "given": "Valérie"}, {"family": "Fabre", "given": "Cécile"}])
    diffs = compare_with_manuscript(ms, lib)
    notes = {d["note"] for d in diffs}
    assert any("lost accents" in n for n in notes) and any("full given names" in n for n in notes)
    assert any("manuscript number" in n for n in notes)
    assert all(d["manuscript_better"] for d in diffs)
    assert compare_with_manuscript(lib, lib) == []


def test_check_latex_reports_manuscript_vs_library_and_drops_manuscript_numbers(svc, tmp_path):
    svc.lib.add_item(dict(AGU_ITEM, page="2006JE002868",
                          author=[{"family": "Cahill", "given": "J."}, {"family": "Lucey", "given": "P."}]),
                     source="zotero", citekey="cahill2007")
    (tmp_path / "refs.bib").write_text(
        "@article{cahill2007, author={Cahill, Joshua T. S. and Lucey, Paul G.}, title={Radiative transfer modeling of "
        "lunar highlands spectral classes and relationship to lunar samples}, journal={JGR}, year={2007}, "
        "volume={112}, pages={E10007}}\n", encoding="utf-8")
    (tmp_path / "paper.tex").write_text("Text \\citep{cahill2007}.\n\\bibliography{refs}\n", encoding="utf-8")
    rep = svc.check_latex(str(tmp_path / "paper.tex"), write_bib="rumil-refs.bib")
    fields = {(d["field"], d["manuscript_better"]) for d in rep["manuscript_vs_library"]}
    assert ("author", True) in fields and ("page", True) in fields
    written = (tmp_path / "rumil-refs.bib").read_text(encoding="utf-8")
    assert "2006JE002868}" not in written.split("doi")[0] and "RUMIL WARNING [page]" in written
    assert "more complete" in rep["summary"]


def test_loosely_typed_bib_entry_links_to_existing_item(svc, tmp_path):
    svc.lib.add_item({"type": "dataset", "title": "Mars 2020 Terrain Relative Navigation HiRISE DTM Mosaic",
                      "author": [{"family": "Fergason", "given": "R. L."}], "issued": "2020"},
                     source="zotero", citekey="fergasonMars2020Terrain2020")
    (tmp_path / "m.bib").write_text("@misc{Fergason2018, author={Fergason, R. L.}, year={2019}, "
                                    "title={Mars 2020 Terrain Relative Navigation {HiRISE} {DTM} Mosaic}}\n", encoding="utf-8")
    rep = svc.import_bibfile(str(tmp_path / "m.bib"), verify=False)
    assert not rep["added"]
    assert rep["aliases_added"] == [{"alias": "Fergason2018", "for": "fergasonMars2020Terrain2020"}]
    # A journal paper and a conference abstract with the same title stay separate.
    svc.lib.add_item({"type": "paper-conference", "title": "Hollows on Mercury", "issued": "2021",
                      "author": [{"family": "Phillips", "given": "M."}], "container-title": "LPSC"}, source="manual")
    (tmp_path / "n.bib").write_text("@article{p21, author={Phillips, M.}, year={2021}, title={Hollows on Mercury}, "
                                    "journal={Icarus}}\n", encoding="utf-8")
    assert svc.import_bibfile(str(tmp_path / "n.bib"), verify=False)["added"]


def test_bibtex_accents_inside_names_survive_import():
    from rumil.importers import parse_bibtex
    t = (r"@article{a, author={Payr{\'e}, Val{\'e}rie and {\"O}zt{\"u}rk, Ay{\c{s}}e and van der Bogert, Carolyn H. "
         r"and {MERTIS Team}}, title={T}, year={2020}}")
    names = parse_bibtex(t)[0][1]["author"]
    assert names[0] == {"family": "Payré", "given": "Valérie"}
    assert names[1] == {"family": "Öztürk", "given": "Ayşe"}
    assert names[2]["family"] == "van der Bogert" and names[3] == {"literal": "MERTIS Team"}


def test_names_garbled_by_the_old_bib_import_are_repaired():
    local = {"title": "Sulfur on Mars", "issued": "2006", "author": [{"family": "Payré", "given": "Val\\ŕie"}]}
    reg = dict(local, author=[{"family": "Payré", "given": "Valérie"}])
    res = _cmp(local, reg)
    assert res.status == "differs"
    fix = next(s for s in res.suggestions if s["field"] == "author")
    assert fix["safe"] and fix["proposed"][0]["given"] == "Valérie"
