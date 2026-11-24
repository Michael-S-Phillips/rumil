"""Second round of feedback from real use: full-title checks, swapped names, rate limits, config gaps, coverage."""

SALT = {"type": "article-journal", "container-title": "Remote Sensing", "volume": "15", "issue": "2", "page": "314",
        "issued": "2023", "DOI": "10.3390/rs15020314",
        "author": [{"family": "Phillips", "given": "Michael S."}, {"family": "McInenly", "given": "Michael"},
                   {"family": "Hofmann", "given": "Michael H."}, {"family": "Hinman", "given": "Nancy W."},
                   {"family": "Warren-Rhodes", "given": "Kimberley"}, {"family": "Rivera-Valentín", "given": "Edgard G."},
                   {"family": "Cabrol", "given": "Nathalie A."}]}


def _add(svc, **over):
    csl = dict(SALT, **over)
    return svc.lib.add_item(csl, source="zotero")["citekey"]


def test_full_title_is_compared_word_for_word(svc):
    # One word changed (like "impact-melt" vs "impact melting"): same work, but the printed title would be wrong.
    k = _add(svc, title="Salt Constructs in Paleo-Lake Basins as High-Priority Astrobiology Target")
    r = svc.verify([k])["results"][0]
    assert r["status"] == "differs"
    fix = next(s for s in r["suggestions"] if s["field"] == "title")
    assert fix["correction"] and not fix["safe"]
    assert fix["proposed"] == "Salt Constructs in Paleo-Lake Basins as High-Priority Astrobiology Targets"
    assert any("Title: library" in i for i in r["issues"])
    # Case, punctuation, hyphen-vs-space, and '&' don't count.
    k2 = _add(svc, title="Salt constructs in paleo lake basins as high-priority astrobiology targets", DOI="10.3390/RS15020314")
    svc.lib.trash(k)
    assert svc.verify([k2])["results"][0]["status"] == "verified"


def test_missing_subtitle_differs(svc):
    k = _add(svc, title="Salt Constructs in Paleo-Lake Basins")
    assert svc.verify([k])["results"][0]["status"] == "differs"


def test_corrections_apply_and_keep_mine(svc):
    k = _add(svc, title="Salt Constructs in Paleo-Lake Basins as High-Priority Astrobiology Target", page="315")
    r = svc.verify([k])["results"][0]
    assert r["status"] == "differs" and {s["field"] for s in r["suggestions"] if s.get("correction")} == {"title", "page"}
    assert svc.apply_fixes(k, "safe")["status"] == "differs"  # safe fixes never overwrite
    out = svc.apply_fixes(k, "corrections")
    assert out["status"] == "verified"
    c = svc.lib.get(k)["csl"]
    assert c["page"] == "314" and c["title"].endswith("Targets")
    # Keeping a deliberate library value stops the flag (until the registry value changes).
    k2 = _add(svc, title="Salt constructs in paleo-lake basins: high-priority astrobiology targets")
    svc.lib.trash(k)
    assert svc.verify([k2])["results"][0]["status"] == "differs"
    kept = svc.apply_fixes(k2, None, keep=["title"])
    assert kept["status"] == "verified"
    assert svc.verify([k2])["results"][0]["status"] == "verified"
    assert any(h["action"] == "keep-library-values" for h in svc.lib.history(k2))


def test_swapped_given_and_family_names_are_a_correction_not_a_mismatch(svc):
    swapped = [{"family": "Michael", "given": "S. Phillips"}, {"family": "Christina", "given": "E. Viviano"},
               {"family": "Jeffrey", "given": "E. Moersch"}, {"family": "A.", "given": "Deanne Rogers"},
               {"family": "Harry", "given": "Y. McSween"}, {"family": "Frank", "given": "P. Seelos"}]
    k = svc.lib.add_item({"type": "article-journal", "title": "Extensive and ancient feldspathic crust detected across north "
                          "Hellas rim, Mars: Possible implications for primary crust formation", "author": swapped,
                          "issued": "2022", "container-title": "Geology", "volume": "50", "page": "1182-1186",
                          "DOI": "10.1130/G50341.1"}, source="zotero")["citekey"]
    r = svc.verify([k])["results"][0]
    assert r["status"] == "differs"
    fix = next(s for s in r["suggestions"] if s["field"] == "author")
    assert "swapped" in fix["reason"]
    svc.apply_fixes(k, "corrections")
    assert svc.lib.get(k)["csl"]["author"][0] == {"family": "Phillips", "given": "Michael S."}


def test_missing_first_author_is_a_correction(svc):
    k = _add(svc, title="Salt Constructs in Paleo-Lake Basins as High-Priority Astrobiology Targets",
             author=SALT["author"][1:])
    r = svc.verify([k])["results"][0]
    assert r["status"] == "differs"
    assert any(s["field"] == "author" and s.get("correction") for s in r["suggestions"])


def test_tex_journal_macro(svc):
    from rumil.csl import clean_item
    assert clean_item({"title": "x", "container-title": "\\maps"})["container-title"] == "Meteoritics & Planetary Science"
    assert clean_item({"title": "x", "container-title": "\\notajournal"})["container-title"] == "\\notajournal"
    from rumil.importers import parse_bibtex
    recs = parse_bibtex("@article{a, title={T}, author={X, Y}, journal={\\icarus}, year={2000}}")
    assert recs[0][1]["container-title"] == "Icarus"


def test_check_latex_flags_differing_entries_in_the_bib(svc, tmp_path):
    from rumil.latex import check_tex
    k = _add(svc, title="Salt Constructs in Paleo-Lake Basins as High-Priority Astrobiology Target")
    svc.verify([k])
    (tmp_path / "p.tex").write_text(r"\citet{%s}" % k)
    rep = check_tex(svc.lib, tmp_path / "p.tex", write_bib="rumil-refs.bib")
    assert rep["problems"] and rep["problems"][0]["status"] == "differs"
    bib = (tmp_path / "rumil-refs.bib").read_text()
    assert "% RUMIL WARNING [differs]" in bib and rep["bib_entries_flagged"] == 1


def test_rate_limited_search_is_an_error_not_unverified(cfg):
    from conftest import FakeTransport
    from rumil.http import Http, Response
    from rumil.service import Rumil

    base = FakeTransport()

    def limited(method, url, headers, timeout):
        if "api.crossref.org/works?" in url:
            return Response(429, url, {"retry-after": "0"}, b"Too many requests")
        return base(method, url, headers, timeout)

    s = Rumil(cfg, http=Http(transport=limited))
    k = s.lib.add_item({"type": "article-journal", "title": "Mercury's global color mosaic: An update from MESSENGER's orbital observations",
                        "author": [{"family": "Domingue"}], "issued": "2015"}, source="zotero")["citekey"]
    r = s.verify([k])["results"][0]
    assert r["status"] == "error" and "rate-limiting" in r["summary"]
    s.lib.close()


def test_crossref_searches_have_their_own_slower_gate():
    from rumil.http import Http
    h = Http()
    assert h._gate_key("https://api.crossref.org/works?query.bibliographic=x") == "api.crossref.org#list"
    assert h._gate_key("https://api.crossref.org/works/10.1130/G50341.1?mailto=x") == "api.crossref.org"
    assert h._limits["api.crossref.org#list"][1] >= 1.0  # public pool: 1 search per second
    polite = Http(contact_email="someone@example.edu")
    assert polite._limits["api.crossref.org#list"][1] <= 0.4 and "mailto:someone@example.edu" in polite.user_agent


def test_lpsc_abstract_verified_without_ads(svc, monkeypatch):
    import rumil.resolvers as R
    text = ("53rd Lunar and Planetary Science Conference (2022) 1234.pdf PLAGIOCLASE-RICH CRUST IN ARGYRE BASIN "
            "MASSIFS. M. S. Phillips1 and J. E. Moersch2")
    monkeypatch.setattr(R, "pdf_text", lambda data, max_pages=2: text)
    k = svc.lib.add_item({"type": "paper-conference", "title": "Plagioclase-rich crust in Argyre basin massifs",
                          "author": [{"family": "Phillips", "given": "M. S."}], "issued": "2022",
                          "container-title": "53rd Lunar and Planetary Science Conference", "page": "1234"}, source="zotero")["citekey"]
    r = svc.verify([k])["results"][0]
    assert r["status"] == "verified_no_doi" and "LPSC 2022 abstract #1234" in r["summary"]
    assert any(s["field"] == "URL" and s["safe"] for s in r["suggestions"])
    # Wrong abstract number: the LPI PDF has a different title, so it is not verified.
    k2 = svc.lib.add_item({"type": "paper-conference", "title": "Something else entirely about Venus",
                           "author": [{"family": "Smith"}], "issued": "2022", "container-title": "LPSC", "page": "1234"},
                          source="zotero")["citekey"]
    r2 = svc.verify([k2])["results"][0]
    assert r2["status"] == "unverified" and any("different title" in i for i in r2["issues"])


def test_lpsc_info():
    from rumil.resolvers import lpsc_info
    assert lpsc_info({"container-title": "57th Lunar and Planetary Science Conference", "page": "2101"}) == (2026, 2101)
    assert lpsc_info({"container-title": "LPI", "issued": {"date-parts": [[2019]]}, "note": "Abstract #1501"}) == (2019, 1501)
    assert lpsc_info({"container-title": "Icarus", "page": "1234", "issued": {"date-parts": [[2019]]}}) is None


def test_setup_warnings_and_config_command(cfg, capsys):
    from rumil.cli import main
    from rumil.config import Config, setup_warnings
    assert len(setup_warnings(cfg)) == 2
    main(["config", "set", "contact_email", "someone@example.edu"])
    main(["config", "set", "ads_token", "secret-token"])
    c = Config(cfg.home)
    assert c.get("contact_email") == "someone@example.edu" and setup_warnings(c) == []
    main(["config", "show"])
    out = capsys.readouterr().out
    assert "secret-token" not in out and '"ads_token": "(set)"' in out


def test_import_verifies_and_check_latex_imports_missing(svc, tmp_path):
    (tmp_path / "paper.tex").write_text(r"\citep{salt2023, geology2022, nowhere2020} \bibliography{references}")
    (tmp_path / "references.bib").write_text(
        "@article{salt2023, author={Phillips, Michael S. and McInenly, Michael}, "
        "title={Salt Constructs in Paleo-Lake Basins as High-Priority Astrobiology Target}, journal={Remote Sensing}, "
        "year={2023}, volume={15}, pages={314}, doi={10.3390/rs15020314}}\n"
        "@article{geology2022, author={Phillips, M. S.}, title={Extensive and ancient feldspathic crust detected across north "
        "Hellas rim, Mars: Possible implications for primary crust formation}, journal={Geology}, year={2022}, "
        "volume={50}, pages={1182--1186}, doi={10.1130/G50341.1}}\n")
    rep = svc.check_latex(str(tmp_path / "paper.tex"), import_missing=True, write_bib="rumil-refs.bib")
    assert [a["citekey"] for a in rep["imported"]["added"]] == ["salt2023", "geology2022"]
    assert rep["imported"]["still_missing"] == ["nowhere2020"]
    assert [m["key"] for m in rep["missing"]] == ["nowhere2020"]
    statuses = {p["key"]: p["status"] for p in rep["problems"]}
    assert statuses == {"salt2023": "differs"}  # the .bib's title is one word off from Crossref
    assert "geology2022" in rep["ok"]
    # Importing a file verifies the new entries.
    (tmp_path / "more.bib").write_text("@article{retracted2020, author={Feng, Wenlong}, title={Myc is a prognostic biomarker "
                                       "and potential therapeutic target in osteosarcoma}, journal={Ther Adv Med Oncol}, "
                                       "year={2020}, doi={10.1177/1758835920922055}}")
    imp = svc.import_bibfile(str(tmp_path / "more.bib"))
    assert imp["verification"]["results"][0]["status"] == "retracted"


def test_cintala_title_error_is_caught_and_fixed(svc):
    """The case from real use: 'impact-melt' in the library vs 'impact melting' at Crossref, journal stored as \\maps."""
    k = svc.lib.add_item({"type": "article-journal",
                          "title": "Scaling impact-melt and crater dimensions: Implications for the lunar cratering record",
                          "author": [{"family": "Cintala", "given": "Mark J."}, {"family": "Grieve", "given": "Richard A. F."}],
                          "issued": "1998", "container-title": "\\maps", "volume": "33", "page": "889-912",
                          "DOI": "10.1111/j.1945-5100.1998.tb01695.x"}, source="zotero")["citekey"]
    assert svc.lib.get(k)["csl"]["container-title"] == "Meteoritics & Planetary Science"
    r = svc.verify([k])["results"][0]
    assert r["status"] == "differs"
    assert r["issues"][0].startswith("Title: library 'Scaling impact-melt and crater dimensions")
    assert {s["field"] for s in r["suggestions"] if s.get("correction")} == {"title"}  # authors match despite CAPS
    svc.apply_fixes(k, "corrections")
    c = svc.lib.get(k)["csl"]
    assert c["title"] == "Scaling impact melting and crater dimensions: Implications for the lunar cratering record"
    assert c["issue"] == "4" and svc.lib.get(k)["verify_status"] == "verified"


def test_registry_names_in_capitals_are_normalized():
    from rumil.resolvers import fix_caps
    assert fix_caps("CINTALA") == "Cintala" and fix_caps("MARK J.") == "Mark J."
    assert fix_caps("RIVERA-VALENTÍN") == "Rivera-Valentín" and fix_caps("MCSWEEN") == "McSween"
    assert fix_caps("DE PATER") == "de Pater" and fix_caps("Phillips") == "Phillips" and fix_caps("LI") == "Li"
    assert fix_caps("JE", given=True) == "JE"  # initials: clean_item turns them into "J. E."
