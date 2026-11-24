"""Third round: the full-title comparison run against a real library. Export debris and registry quirks must not
be reported as errors, while real differences (a changed word, a wrong volume, a wrong author) still are."""

import pytest

from rumil.resolvers import Resolution
from rumil.textutil import family_matches, split_running_head, strip_database_tags, title_words
from rumil.verify import VerifyResult, _compare_number

from test_feedback_round2 import SALT, _add

TITLE = "Salt Constructs in Paleo-Lake Basins as High-Priority Astrobiology Targets"


def _cmp(verifier, local: dict, reg: dict, source="crossref"):
    res = VerifyResult(status="unverified", summary="")
    verifier._compare(local, Resolution(reg, source, "", {}), res)
    return res


@pytest.fixture
def verifier(svc):
    return svc.verifier


def test_database_tag_in_library_title_is_a_safe_fix(svc):
    k = _add(svc, title=TITLE + ".(Report)(Author abstract)")
    r = svc.verify([k])["results"][0]
    assert r["status"] == "differs"
    fix = next(s for s in r["suggestions"] if s["field"] == "title")
    assert fix["safe"] and fix["proposed"] == TITLE
    assert svc.apply_fixes(k, "safe")["status"] == "verified"
    assert svc.lib.get(k)["csl"]["title"] == TITLE


def test_running_head_in_library_title_is_a_safe_fix(svc):
    k = _add(svc, title=TITLE + ": SALT CONSTRUCTS AS TARGETS")
    r = svc.verify([k])["results"][0]
    assert r["status"] == "differs"
    fix = next(s for s in r["suggestions"] if s["field"] == "title")
    assert fix["safe"] and fix["proposed"] == TITLE


def test_library_subtitle_missing_from_registry_is_only_noted(svc):
    k = _add(svc, title=TITLE + ": a review of terrestrial analogs")
    r = svc.verify([k])["results"][0]
    assert r["status"] == "verified"
    assert any("stops before the library's subtitle" in i for i in r["issues"])


@pytest.mark.parametrize("field,value,expected", [
    ("page", "n/a", "314"), ("page", "n/a-n/a", "314"), ("volume", "0", "15"), ("issue", "Volume 15, 2023", "2"),
])
def test_placeholder_numbers_are_safe_fixes(svc, field, value, expected):
    k = _add(svc, title=TITLE, **{field: value})
    r = svc.verify([k])["results"][0]
    assert r["status"] == "differs"
    fix = next(s for s in r["suggestions"] if s["field"] == field)
    assert fix["safe"] and fix["proposed"] == expected
    assert svc.apply_fixes(k, "safe")["status"] == "verified"


def test_running_head_on_registry_side_is_ignored(verifier):
    local = {"title": "Very recent and wide-spread basaltic volcanism on Mars", "author": [{"family": "Hauber", "given": "E."}]}
    reg = dict(local, title="Very recent and wide-spread basaltic volcanism on Mars: RECENT WIDE-SPREAD VOLCANISM ON MARS")
    assert _cmp(verifier, local, reg).status == "verified"
    reg["title"] = ("Characterization of space weathering from Lunar Reconnaissance Orbiter Camera ultraviolet "
                    "observations of the Moon: Space weathering from LROC UV")
    local["title"] = reg["title"].split(": Space")[0]
    assert _cmp(verifier, local, reg).status == "verified"


def test_split_running_head_keeps_real_subtitles():
    for real in ("Scaling impact melting and crater dimensions: Implications for the lunar cratering record",
                 "The Habitat Concept in Ornithology: Theory and Applications",
                 "SHADOWS: a spectro-gonio radiometer for bidirectional reflectance studies of dark meteorites and "
                 "terrestrial analogs: design, calibrations, and performances on challenging surfaces",
                 "Investigating potential sources of Mercury's exospheric Calcium: Photon-stimulated desorption of "
                 "Calcium Sulfide"):
        assert split_running_head(real)[1] == ""
    assert split_running_head("The biogenicity of modern terrestrial roll-up structures and its significance for "
                              "ancient life on land: Roll-ups as biosignatures")[1] == "Roll-ups as biosignatures"
    assert strip_database_tags("Images of surface volatiles.(Author abstract)") == "Images of surface volatiles"
    assert strip_database_tags("A survey (Survey) of craters") == "A survey (Survey) of craters"


def test_html_entities_in_titles_are_not_word_differences():
    assert title_words("ancient crust") == title_words("ancient crust.&#160;")
    assert title_words("Hadean crust (>4370 Ma)") == title_words("Hadean crust (&gt;4370 Ma)")
    assert title_words("Rocks & minerals") == title_words("Rocks &amp; minerals") == title_words("Rocks and minerals")


@pytest.mark.parametrize("k,lib,reg,kind", [
    ("issue", "1", "1-4", "fill"), ("issue", "3", "3-4", "fill"), ("issue", "1-4", "1", "same"),
    ("issue", "1", "01", "same"), ("issue", "8", "E8", "same"), ("issue", "S1", "1_suppl", "same"),
    ("issue", "1–2", "1-2", "same"), ("issue", "11", "12", "diff"), ("issue", "Volume 40, 2012", "1", "junk"),
    ("volume", "XLII-3-W1", "XLII-3/W1", "same"), ("volume", "371–372", "371-372", "same"),
    ("volume", "0", "391", "junk"), ("volume", "121", "124", "diff"),
    ("page", "1093", "1093-1112", "fill"), ("page", "228-36", "228-236", "fill"), ("page", "5717-&", "5717-5720", "junk"),
    ("page", "344-+", "344-348", "junk"), ("page", "n/a", "e2020JE006536", "junk"), ("page", "E10007-n/a", "2006JE002868", "junk"),
    ("page", "eaap8306-eaap8306", "eaap8306", "fill"), ("page", "111,IN1,119-118,IN1,141", "111-141", "same"),
    ("page", "1-52", "2", "same"), ("page", "e2308074", "1-12", "same"), ("page", "084988", "1-17", "same"),
    ("page", "E09004", "2009JE003495", "note"), ("page", "1-7", "456-462", "diff"), ("page", "18", "3354-3372", "diff"),
])
def test_compare_number(k, lib, reg, kind):
    assert _compare_number(k, lib, reg)[0] == kind


def test_spie_article_numbers_match_crossref_paper_numbers():
    assert _compare_number("page", "111280T", "2", "11128")[0] == "same"
    assert _compare_number("page", "111280T", "2")[0] == "diff"


def test_cleaned_page_keeps_the_library_citation_number():
    assert _compare_number("page", "E10007-n/a", "2006JE002868") == ("junk", "E10007")


def test_author_lists_ignore_group_names_and_particles(verifier):
    people = [{"family": "Wiens", "given": "R. C."}, {"family": "von Arx", "given": "Sydney"}]
    local = {"title": "A petrological traverse of the olivine cumulate", "author": people + [{"family": "Team", "given": "The SuperCam"}]}
    reg = dict(local, author=[{"family": "Wiens", "given": "R. C."}, {"family": "Arx", "given": "Sydney von"},
                              {"literal": "Third-Party Scientists"}])
    assert _cmp(verifier, local, reg).status == "verified"
    assert family_matches("von Arx", "Arx") and not family_matches("Cremonese", "Cremosese")


def test_registry_that_lists_only_the_first_author_is_noted(verifier):
    local = {"title": "The evolution of the Martian hydrosphere", "author": [{"family": "Clifford", "given": "Stephen M."},
                                                                             {"family": "Parker", "given": "Timothy J."}]}
    reg = dict(local, author=[{"family": "Clifford", "given": "S"}])
    res = _cmp(verifier, local, reg)
    assert res.status == "verified" and any("only the first author" in i for i in res.issues)
    # A registry with two names and a library with three is still a difference worth checking.
    local["author"] = local["author"] + [{"family": "Haberle", "given": "Robert M."}]
    reg["author"] = local["author"][:2]
    res = _cmp(verifier, local, reg)
    assert res.status == "differs" and any(s.get("correction") and s["field"] == "author" for s in res.suggestions)


def test_et_al_with_a_different_first_author_is_not_a_safe_fill(verifier):
    local = {"title": "Characterising the new DLR planetary emissivity laboratory", "author": [
        {"family": "Lukas", "given": "J."}, {"family": "Maturilli", "given": "A."}, {"literal": "others"}]}
    reg = dict(local, author=[{"family": "Helbert", "given": "Jörn"}, {"family": "Lukas", "given": "J."},
                              {"family": "Maturilli", "given": "Alessandro"}])
    res = _cmp(verifier, local, reg)
    assert not any(s.get("safe") and s["field"] == "author" for s in res.suggestions)
    assert res.status == "differs"


def test_real_differences_are_still_reported(svc):
    k = _add(svc, title=TITLE, volume="14", issue="3")
    r = svc.verify([k])["results"][0]
    assert r["status"] == "differs"
    assert {s["field"] for s in r["suggestions"] if s.get("correction")} == {"volume", "issue"}
    assert not any(s.get("safe") for s in r["suggestions"] if s["field"] in ("volume", "issue"))
    assert "volume, issue differ" in r["summary"]


def test_salt_record_is_untouched_by_the_new_rules(svc):
    assert SALT["page"] == "314"
    k = _add(svc, title=TITLE)
    assert svc.verify([k])["results"][0]["status"] == "verified"


def test_keep_mine_works_for_debris_fixes_too(svc):
    k = _add(svc, title=TITLE + ": SALT CONSTRUCTS AS TARGETS")
    r = svc.verify([k])["results"][0]
    assert next(s for s in r["suggestions"] if s["field"] == "title")["keepable"]
    assert svc.apply_fixes(k, None, keep=["title"])["status"] == "verified"
    assert svc.lib.get(k)["csl"]["title"].endswith("AS TARGETS")


def test_lpsc_style_exists_and_is_compact(svc):
    add = lambda c: svc.lib.add_item(c, source="manual")["citekey"]
    k1 = add({"type": "article-journal", "title": "Extensive and ancient feldspathic crust", "issued": "2022",
              "author": [{"family": "Phillips", "given": "Michael S."}, {"family": "Moersch", "given": "Jeffrey E."},
                         {"family": "Viviano", "given": "Christina E."}],
              "container-title": "Journal of Geophysical Research: Planets", "container-title-short": "JGR Planets",
              "volume": "127", "page": "e2021JE007018"})
    k2 = add({"type": "paper-conference", "title": "Salt constructs", "issued": "2022", "number": "1234",
              "author": [{"family": "Phillips", "given": "Michael S."}, {"family": "Hinman", "given": "Nancy W."}],
              "container-title": "53rd Lunar and Planetary Science Conference"})
    r = svc.formatter.cite_strings([f"[@{k1}; @{k2}]"], "lpsc")
    assert r["citations"][0]["text"] == "[1, 2]"
    bib = [b["text"] for b in r["bibliography"]]
    assert bib[0] == "[1] Phillips M. S. et al. (2022) JGR Planets, 127, e2021JE007018."
    assert bib[1] == "[2] Phillips M. S. and Hinman N. W. (2022) 53rd Lunar and Planetary Science Conference, Abstract #1234."
