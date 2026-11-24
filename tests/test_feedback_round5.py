"""Fifth round, from re-checking a review paper's 274 references: an erratum's DOI offered as the article's, a
start-page-only reference that verified without its full range, names split wrongly around Jr. and III, and family
names that print differently but were treated as equal."""

import pytest

from rumil.bibtex import bib_entry
from rumil.csl import clean_item, parse_name
from rumil.importers import parse_bibtex

TITLE = "Evidence for magmatic evolution and diversity on Mars from infrared observations"
CHRISTENSEN = {"type": "article-journal", "title": TITLE, "issued": "2005", "container-title": "Nature",
               "volume": "436", "author": [{"family": "Christensen", "given": "Philip R."},
                                           {"family": "McSween", "given": "Harry Y.", "suffix": "Jr."},
                                           {"family": "Bandfield", "given": "Joshua L."},
                                           {"family": "Ruff", "given": "Steven W."},
                                           {"family": "Rogers", "given": "A. Deanne"}]}


@pytest.mark.parametrize("raw", ["McSween, Jr., Harry Y.", "McSween, Harry Y., Jr.", "McSween Jr., Harry Y.",
                                 "Harry Y. McSween Jr.", "Harry Y. McSween, Jr.", "McSween, Harry Y. Jr."])
def test_jr_in_every_position(raw):
    assert parse_name(raw) == {"family": "McSween", "given": "Harry Y.", "suffix": "Jr."}


def test_suffixes_survive_bibtex_round_trip_and_never_become_initials():
    t = r"@article{a, author={McSween, Jr., Harry Y. and Irwin, III, Rossman P.}, title={T}, year={2020}}"
    names = parse_bibtex(t)[0][1]["author"]
    assert names == [{"family": "McSween", "given": "Harry Y.", "suffix": "Jr."},
                     {"family": "Irwin", "given": "Rossman P.", "suffix": "III"}]
    assert "McSween, Jr., Harry Y. and Irwin, III, Rossman P." in bib_entry("a", {"type": "article-journal", "author": names})
    assert clean_item({"title": "x", "author": [{"family": "Iii", "given": "Rossman P. Irwin"}]})["author"] == \
        [{"family": "Irwin", "given": "Rossman P.", "suffix": "III"}]
    assert parse_name("A. V. Smith") == {"family": "Smith", "given": "A. V."}  # a lone V is an initial


def test_suffix_in_the_wrong_place_is_repaired_on_verification(svc):
    bad = [dict(n) for n in CHRISTENSEN["author"]]
    bad[1] = {"family": "Jr.", "given": "Harry Y. McSween"}
    k = svc.lib.add_item(dict(CHRISTENSEN, DOI="10.1038/nature03639", page="504-509", author=bad),
                         source="zotero")["citekey"]
    import json  # store the record the way an older version left it (saving now repairs names on the way in)
    it = svc.lib.get(k)
    with svc.lib._lock:
        svc.lib.conn.execute("UPDATE items SET csl = ? WHERE id = ?", (json.dumps(dict(it["csl"], author=bad)), it["id"]))
        svc.lib.conn.commit()
    assert svc.lib.get(k)["csl"]["author"][1]["family"] == "Jr."
    r = svc.verify([k])["results"][0]
    assert r["status"] == "differs"
    assert svc.apply_fixes(k, "corrections")["status"] == "verified"
    assert svc.lib.get(k)["csl"]["author"][1] == {"family": "McSween", "given": "Harry Y.", "suffix": "Jr."}


def test_family_name_that_prints_differently_is_a_difference(svc):
    wrong = [dict(n) for n in CHRISTENSEN["author"]]
    wrong[4] = {"family": "Deanne Rogers", "given": "A."}
    k = svc.lib.add_item(dict(CHRISTENSEN, DOI="10.1038/nature03639", page="504-509", author=wrong),
                         source="zotero")["citekey"]
    r = svc.verify([k])["results"][0]
    assert r["status"] == "differs"
    fix = next(s for s in r["suggestions"] if s["field"] == "author")
    assert fix["correction"] and fix["proposed"][4] == {"family": "Rogers", "given": "A. Deanne"}


def test_search_never_offers_an_erratum_as_the_article(svc):
    k = svc.lib.add_item(dict(CHRISTENSEN), source="zotero")["citekey"]
    r = svc.verify([k])["results"][0]
    dois = [s["proposed"] for s in r["suggestions"] if s["kind"] == "add_doi"]
    assert dois == ["10.1038/nature03639"]
    assert "nature04075" not in " ".join(r["issues"])


def test_erratum_found_by_update_to_link_leads_to_the_article(svc, fake):
    fake.christensen_search = "crossref_search_christensen_linked.json"  # only the erratum comes back
    k = svc.lib.add_item(dict(CHRISTENSEN), source="zotero")["citekey"]
    r = svc.verify([k])["results"][0]
    assert [s["proposed"] for s in r["suggestions"] if s["kind"] == "add_doi"] == ["10.1038/nature03639"]


def test_erratum_doi_in_the_library_is_flagged(svc):
    k = svc.lib.add_item(dict(CHRISTENSEN, DOI="10.1038/nature04075", page="882"), source="zotero")["citekey"]
    r = svc.verify([k])["results"][0]
    assert r["status"] == "mismatch" and "correction or retraction notice" in r["summary"]
    fix = next(s for s in r["suggestions"] if s["kind"] == "replace_doi")
    assert fix["proposed"] == "10.1038/nature03639" and not fix["safe"]
    assert not any(s.get("field") == "page" for s in r["suggestions"])  # nothing copied from the notice


def test_found_doi_then_full_page_range_in_one_apply(svc):
    k = svc.lib.add_item(dict(CHRISTENSEN, page="504"), source="zotero")["citekey"]
    r = svc.verify([k])["results"][0]
    assert r["status"] == "doi_found"
    out = svc.apply_fixes(k, "safe")
    c = svc.lib.get(k)["csl"]
    assert out["status"] == "verified" and c["DOI"] == "10.1038/nature03639" and c["page"] == "504-509"


def test_verified_but_incomplete_is_reported(svc, tmp_path):
    k = svc.lib.add_item(dict(CHRISTENSEN, DOI="10.1038/nature03639", page="504"), source="zotero",
                         citekey="christensen2005")["citekey"]
    r = svc.verify([k])["results"][0]
    assert r["status"] == "verified" and "safe fixes available" in r["summary"] and "pages" in r["summary"]
    (tmp_path / "p.tex").write_text("\\citep{christensen2005}\n", encoding="utf-8")
    rep = svc.check_latex(str(tmp_path / "p.tex"))
    assert rep["fixable"][0]["key"] == "christensen2005" and "safe fixes waiting" in rep["summary"]


def _cmp(local, reg, source="crossref"):
    from rumil.http import Http
    from rumil.resolvers import Resolution, Resolver
    from rumil.verify import Verifier, VerifyResult, _suffix_repair
    ver = Verifier.__new__(Verifier)
    ver.resolver = Resolver(Http(transport=lambda *a: None))
    res = VerifyResult(status="unverified", summary="")
    ver._compare(local, Resolution(reg, source, "", {}), res)
    _suffix_repair(local, res)
    return res


def test_suffix_inside_family_is_a_quiet_tidy_up():
    local = {"title": "Northern lowlands of Mars", "issued": "2002",
             "author": [{"family": "Head III", "given": "James W."}, {"family": "Kreslavsky", "given": "M. A."}]}
    reg = dict(local, author=[{"family": "Head", "given": "James W.", "suffix": "III"},
                              {"family": "Kreslavsky", "given": "M. A."}])
    res = _cmp(local, reg)
    assert res.status == "verified"
    fix = next(s for s in res.suggestions if s.get("field") == "author")
    assert fix["safe"] and fix["proposed"][0] == {"family": "Head", "given": "James W.", "suffix": "III"}


def test_registry_quirks_in_names_are_not_differences():
    local = {"title": "Non-uniform cratering", "issued": "2008",
             "author": [{"family": "Le Feuvre", "given": "Mathieu"}, {"family": "Wieczorek", "given": "Mark A."}]}
    assert _cmp(local, dict(local, author=[{"family": "LEFEUVRE", "given": "M"},
                                           {"family": "WIECZOREK", "given": "M"}])).status == "verified"
    local = {"title": "HyPyRameter", "issued": "2024",
             "author": [{"family": "Phillips", "given": "Michael"}, {"family": "Udovicic", "given": "Christian J. Tai"}]}
    reg = dict(local, author=[{"family": "Michael Phillips"}, {"family": "Christian J. Tai Udovicic"}])
    assert _cmp(local, reg, "datacite").status == "verified"
    orgs = {"title": "Origins, Worlds, and Life", "issued": "2022",
            "author": [{"literal": "Space Studies Board"}, {"literal": "National Academies of Sciences, Engineering, and Medicine"}]}
    assert _cmp(orgs, dict(orgs)).status == "verified"
