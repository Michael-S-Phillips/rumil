"""Keep mine when the registry record is incomplete or lists an organisation as an author."""


def test_untitled_registry_record_is_not_a_mismatch_and_title_can_be_kept(svc):
    k = svc.lib.add_item({"type": "article-journal", "title": "Inflation rates, rifts, and bands in a pāhoehoe sheet flow",
                          "author": [{"family": "Hoblitt", "given": "Richard"}, {"family": "Orr", "given": "Tim R."}],
                          "issued": "2012", "container-title": "Geosphere", "volume": "8", "issue": "1",
                          "page": "179-195", "DOI": "10.1130/GES00656.1"}, source="user")["citekey"]
    r = svc.verify([k])["results"][0]
    assert r["status"] != "mismatch"
    assert any("has no title" in i for i in r["issues"])
    kept = svc.apply_fixes(k, None, keep=["title"])   # used to raise: "The registry record has no 'title' to differ from"
    assert kept["status"] != "mismatch"
    again = svc.verify([k])["results"][0]
    assert not any("has no title" in i for i in again["issues"])
    assert svc.lib.get(k)["csl"]["title"] == "Inflation rates, rifts, and bands in a pāhoehoe sheet flow"


def test_kept_author_list_with_an_organisation_stays_kept(svc):
    people = [{"family": "de Wet", "given": "Andrew P."}, {"family": "Hamilton", "given": "Christopher W."},
              {"family": "Scheidt", "given": "Stephen"}]
    k = svc.lib.add_item({"type": "paper-conference", "title": "LARGE CHANNEL-FED ROOTLESS VOLCANIC CONE COMPLEX, LAKI, ICELAND",
                          "author": people, "issued": "2017",
                          "container-title": "Geological Society of America Abstracts with Programs",
                          "DOI": "10.1130/abs/2017CD-293045"}, source="user")["citekey"]
    r = svc.verify([k])["results"][0]
    assert r["status"] == "differs" and any(s["field"] == "author" for s in r["suggestions"])
    svc.apply_fixes(k, None, keep=["author"])
    again = svc.verify([k])["results"][0]
    assert again["status"] == "verified"
    assert not any(s.get("field") == "author" for s in again["suggestions"])
    assert svc.lib.get(k)["csl"]["author"] == people
