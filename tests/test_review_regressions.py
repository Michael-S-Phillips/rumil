"""Regression tests for problems found in code review: wrong-work matches, silent data loss, injection."""

import pytest

from rumil.csl import clean_item
from rumil.resolvers import _confidence
from rumil.textutil import title_in_text, title_similarity


def test_numbered_sibling_titles_never_match():
    a = "Hollows on Mercury: global classification, Paper I: Morphology"
    b = "Hollows on Mercury: global classification, Paper II: Morphology"
    assert title_similarity(a, b) <= 0.8
    assert title_similarity("Results from sols 1-100", "Results from sols 101-200") <= 0.8
    assert title_similarity(a, a.upper()) == 1.0


def test_confidence_needs_author_and_year():
    assert _confidence(1.0, True, 0) == "high"
    assert _confidence(1.0, None, 0) == "medium"   # no author to compare (generic titles)
    assert _confidence(1.0, True, None) == "medium"  # no year to compare
    assert _confidence(1.0, False, 0) == "low"


def test_mismatch_offers_no_safe_fills(svc):
    it = svc.lib.add_item({"type": "article-journal", "title": "A paper about Venus clouds", "author": [{"family": "Smith"}],
                           "DOI": "10.1130/G50341.1"}, source="manual")
    out = svc.verify([it["citekey"]])["results"][0]
    assert out["status"] == "mismatch"
    assert not any(s["safe"] for s in out["suggestions"])
    fixed = svc.apply_fixes(it["citekey"])  # "safe" set is empty: nothing changes
    assert svc.lib.get(it["citekey"])["csl"].get("container-title") is None and fixed["status"] == "mismatch"


def test_abstract_and_journal_paper_are_different_works(svc):
    svc.lib.add_item({"type": "paper-conference", "title": "Salt Constructs in Paleo-Lake Basins as High-Priority Astrobiology Targets",
                      "author": [{"family": "Phillips", "given": "Michael S."}], "issued": "2022",
                      "container-title": "53rd Lunar and Planetary Science Conference"}, source="manual", citekey="phillips2022lpsc")
    r = svc.add("10.3390/rs15020314")
    assert r["status"] == "added" and r["item"]["citekey"] != "phillips2022lpsc"
    assert svc.lib.count() == 2


def test_bib_import_never_aliases_on_title_alone(svc, tmp_path):
    from rumil.importers import import_file
    svc.lib.add_item({"type": "paper-conference", "title": "Feldspathic crust in Hellas", "author": [{"family": "Phillips"}],
                      "issued": "2020"}, source="manual", citekey="phillips2020lpsc")
    bib = tmp_path / "x.bib"
    bib.write_text("@article{phillips2021journal, author={Phillips, M.}, title={Feldspathic crust in Hellas}, journal={Icarus}, year={2021}}")
    rep = import_file(svc.lib, bib)
    assert rep["added"] and not rep["aliases_added"]
    assert svc.lib.get("phillips2021journal")["csl"]["type"] == "article-journal"


def test_bib_import_reports_key_that_names_another_work(svc, tmp_path):
    from rumil.importers import import_file
    svc.add("10.1130/G50341.1")
    svc.lib.add_item({"type": "book", "title": "Unrelated book", "author": [{"family": "Jones"}], "issued": "1990"},
                     source="manual", citekey="jones2020")
    bib = tmp_path / "x.bib"
    bib.write_text("@article{jones2020, author={Phillips, M. S.}, title={Extensive and ancient feldspathic crust}, "
                   "journal={Geology}, year={2022}, doi={10.1130/G50341.1}}")
    rep = import_file(svc.lib, bib)
    assert rep["key_conflicts"] and rep["key_conflicts"][0]["matches"] == "phillipsExtensiveAncientFeldspathic2022"


def test_formatted_html_is_escaped(svc):
    svc.lib.add_item({"type": "article-journal", "title": "Evil <img src=x onerror=alert(1)> title",
                      "author": [{"family": "O'Neil & <b>Co</b>"}], "issued": "2020", "container-title": "J <script>x</script>",
                      "publisher": "<img src=y>"}, source="manual", citekey="evil2020")
    r = svc.formatter.bibliography(["evil2020"], "agu", output="html")
    h = r["bibliography"][0]["html"]
    assert "<img" not in h and "<script" not in h and "<b>" not in h
    assert "&lt;b&gt;Co&lt;/b&gt;" in h  # markup in data is shown as text, never interpreted
    t = svc.formatter.bibliography(["evil2020"], "agu", output="text")["bibliography"][0]["text"]
    assert t.startswith("O'Neil & <b>Co</b>")


def test_strip_markup_keeps_inequalities():
    from rumil.textutil import strip_markup
    assert strip_markup("Grains <2 mm and >1 mm in <i>situ</i>") == "Grains <2 mm and >1 mm in situ"


def test_clean_item_handles_messy_input():
    c = clean_item({"title": ["A title", "alt"], "author": "Smith, J.; Jones, K.", "issued": {"date-parts": [[None]]},
                    "accessed": {"raw": "2019-05"}, "volume": 5, "editor": {"family": "X"}, "type": "journal-article"})
    assert c["title"] == "A title" and len(c["author"]) == 2 and "issued" not in c
    assert c["accessed"] == {"date-parts": [[2019, 5]]} and c["volume"] == "5" and c["editor"] == [{"family": "X"}]
    assert clean_item({"title": "T", "issued": {"date-parts": [["2020", "13"]]}})["issued"] == {"date-parts": [[2020]]}


def test_messy_record_does_not_break_rendering(svc):
    svc.lib.add_item({"type": "article-journal", "title": "Fine", "author": [{"family": "Ok"}], "issued": "2020"},
                     source="manual", citekey="ok2020")
    svc.lib.conn.execute("UPDATE items SET csl = ? WHERE citekey = 'ok2020'",
                         ('{"type": "article-journal", "title": "Fine", "author": [{"family": "Ok"}], "issued": {"date-parts": [[null]]}}',))
    r = svc.formatter.cite_strings(["[@ok2020]"], "agu")
    assert r["citations"][0]["text"].startswith("(Ok")


def test_check_latex_never_replaces_users_bib(svc, tmp_path):
    from rumil.latex import check_tex
    svc.add("10.1130/G50341.1")
    (tmp_path / "p.tex").write_text(r"\Citet{phillipsExtensiveAncientFeldspathic2022} \bibliography{references}")
    (tmp_path / "references.bib").write_text("@article{mine, title={Hand-made entry}}")
    with pytest.raises(FileExistsError):
        check_tex(svc.lib, tmp_path / "p.tex", write_bib="references.bib")
    assert "Hand-made entry" in (tmp_path / "references.bib").read_text()
    rep = check_tex(svc.lib, tmp_path / "p.tex", write_bib="rumil-refs.bib")
    assert rep["cited"] == 1 and rep["ok"] == ["phillipsExtensiveAncientFeldspathic2022"]  # \Citet is recognized
    check_tex(svc.lib, tmp_path / "p.tex", write_bib="rumil-refs.bib")  # regenerating Rumil's own file is fine


def test_collection_named_like_a_number(svc):
    a = svc.lib.create_collection("First")
    b = svc.lib.create_collection(str(a["id"]))  # a collection literally named "1"
    it = svc.lib.add_item({"type": "book", "title": "B", "author": [{"family": "Z"}], "issued": "2000"}, source="manual")
    svc.lib.add_to_collection(str(a["id"]), [it["id"]])
    assert svc.lib.collection_item_ids(b["id"]) == [it["id"]]
    assert svc.lib.collection_item_ids(a["id"]) == []


def test_merge_resets_verification_when_fields_change(svc):
    a = svc.lib.add_item({"type": "book", "title": "Planetary sciences", "author": [{"family": "de Pater"}], "issued": "2010"},
                         source="manual")
    svc.lib.set_verification(a["id"], "verified_no_doi", {"summary": "x"}, [])
    b = svc.lib.add_item({"type": "book", "title": "Planetary Sciences", "author": [{"family": "de Pater"}], "issued": "2010",
                          "ISBN": "9780521853712"}, source="manual")
    m = svc.lib.merge(a["id"], [b["id"]])
    assert m["verify_status"] == "unchecked" and m["csl"]["ISBN"] == "9780521853712"
    assert svc.lib.get(b["citekey"])["citekey"] == a["citekey"]  # removed key still resolves


def test_pdf_is_identified_by_its_own_title(svc, monkeypatch):
    import rumil.resolvers as R
    text = ("Remote Sens. 2023, 15, 314. Salt Constructs in Paleo-Lake\nBasins as High-Priority Astro-\nbiology Targets\n"
            "References: see doi:10.1130/G50341.1 for an earlier result. This paper: https://doi.org/10.3390/rs15020314")
    monkeypatch.setattr(R, "pdf_text", lambda data, max_pages=2: text)
    r = svc.resolver._from_pdf_bytes(b"%PDF-1.4", "https://example.org/x.pdf")
    assert r.csl["DOI"] == "10.3390/rs15020314"  # not the first DOI on the page (a cited paper)
    monkeypatch.setattr(R, "pdf_text", lambda data, max_pages=2: "Only a citation: 10.1130/G50341.1")
    with pytest.raises(R.NotFoundError):
        svc.resolver._from_pdf_bytes(b"%PDF-1.4", "https://example.org/y.pdf")


def test_title_in_text_tolerates_pdf_line_breaks():
    assert title_in_text("Salt Constructs in Paleo-Lake Basins as High-Priority Astrobiology Targets",
                         "Salt Constructs in Paleo-Lake\nBasins as High-Priority Astro-\nbiology Targets")
    assert not title_in_text("Salt Constructs in Paleo-Lake Basins", "Something else entirely about Mercury hollows")


def test_attachments_never_render_html_on_app_origin(svc, tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import rumil.web as web
    monkeypatch.setattr(web, "_svc", svc)
    it = svc.lib.add_item({"type": "webpage", "title": "Snapshot"}, source="manual")
    page = tmp_path / "snap.html"
    page.write_text("<script>fetch('/api/trash/empty',{method:'POST'})</script>")
    att = svc.lib.add_attachment(it["id"], title="Snapshot", path=str(page), content_type="text/html")
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    att2 = svc.lib.add_attachment(it["id"], title="PDF", path=str(pdf), content_type="application/pdf")
    c = TestClient(web.build_app(23120), base_url="http://127.0.0.1:23120")
    r = c.get(f"/api/attachments/{att}/file")
    assert r.headers["content-type"] == "application/octet-stream" and "attachment" in r.headers["content-disposition"]
    assert r.headers["content-security-policy"] == "sandbox"
    r2 = c.get(f"/api/attachments/{att2}/file")
    assert r2.headers["content-type"] == "application/pdf"


def test_style_install_rejects_paths(svc):
    with pytest.raises(LookupError):
        svc.styles.install("../../evil")


def test_formula_titles_still_match():
    assert title_similarity("Detection of H2O ice in Mercury's polar craters",
                            "Detection of H<sub>2</sub>O ice in Mercury’s polar craters") == 1.0
    assert title_similarity("CO2 frost and δ18O", "CO<sub>2</sub> frost and δ<sup>18</sup>O") == 1.0


def test_organization_names_are_not_split():
    c = clean_item({"title": "T", "author": "Lunar and Planetary Institute"})
    assert c["author"] == [{"literal": "Lunar and Planetary Institute"}]
    c2 = clean_item({"title": "T", "author": "Smith, J. and Jones, K."})
    assert c2["author"] == [{"family": "Smith", "given": "J."}, {"family": "Jones", "given": "K."}]


def test_same_title_at_two_meetings_is_two_works(svc):
    base = {"type": "paper-conference", "title": "Plagioclase-rich crust in Argyre basin massifs",
            "author": [{"family": "Phillips"}], "issued": "2022"}
    svc.lib.add_item(dict(base, **{"container-title": "53rd Lunar and Planetary Science Conference"}), source="manual",
                     citekey="phillips2022lpsc")
    agu = dict(base, **{"container-title": "AGU Fall Meeting 2022"})
    assert svc.lib.find_duplicates_of(agu, strong_only=True) == []
    same = dict(base, **{"container-title": "53rd Lunar and Planetary Science Conference"})
    assert svc.lib.find_duplicates_of(same, strong_only=True)[0][0]["citekey"] == "phillips2022lpsc"


def test_top_level_collection_moved_in_rumil_stays_moved(svc, minizotero):
    from rumil.zotero import import_zotero
    import_zotero(svc.lib)
    proposals = svc.lib.create_collection("Proposals")
    svc.lib.move_collection("hollows_SCUBED", proposals["id"])
    import_zotero(svc.lib)
    assert svc.lib.collection("hollows_SCUBED")["path"] == "Proposals/hollows_SCUBED"
