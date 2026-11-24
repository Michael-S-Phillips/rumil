from pathlib import Path

from rumil.bibtex import bib_entry, protect_title, to_bibtex
from rumil.importers import import_file, parse_bibtex, parse_ris
from rumil.formatting import parse_cite_string


def _seed(svc):
    svc.add("10.3390/rs15020314")
    svc.add("10.1130/G50341.1")
    svc.lib.add_item({"type": "article-journal", "title": "Second Phillips et al. paper from 2023 about Jezero",
                      "author": [{"family": "Phillips"}, {"family": "Smith"}, {"family": "Jones"}], "issued": "2023",
                      "container-title": "Icarus", "volume": "400", "page": "115555"}, source="manual", citekey="phillips2023jezero")
    svc.lib.add_item({"type": "book", "title": "Planetary sciences", "author": [{"family": "de Pater", "given": "Imke"},
                     {"family": "Lissauer", "given": "Jack J."}], "issued": "2010", "publisher": "Cambridge University Press",
                      "edition": "2"}, source="manual", citekey="depater2010")


def test_title_protection():
    assert protect_title("Extensive and ancient feldspathic crust detected across north Hellas rim, Mars: Possible implications") == \
        "Extensive and ancient feldspathic crust detected across north {Hellas} rim, {Mars}: Possible implications"
    t = protect_title("Plagioclase-Rich Crust on Mars Revealed in Argyre Basin Massifs Using CRISM")
    assert "{Mars}" in t and "{Argyre}" in t and "{CRISM}" in t and "{Plagioclase-Rich}" not in t
    assert protect_title("HiRISE and SiO2 & H2O") == "{HiRISE} and {SiO2} \\& {H2O}"


def test_bibtex_export_and_roundtrip(svc):
    _seed(svc)
    items = list(svc.lib.iter_items())
    bib = to_bibtex(items)
    assert "@article{phillipsSaltConstructsPaleoLake2023," in bib
    assert "doi = {10.3390/rs15020314}" in bib
    assert "author = {Phillips, Michael S. and McInenly, Michael" in bib
    assert "@book{depater2010," in bib and "author = {de Pater, Imke and Lissauer, Jack J.}" in bib
    back = {k: c for k, c, _ in parse_bibtex(bib)}
    assert set(back) == {it["citekey"] for it in items}
    assert back["phillipsSaltConstructsPaleoLake2023"]["author"][5]["family"] == "Rivera-Valentín"
    assert back["phillipsExtensiveAncientFeldspathic2022"]["title"].startswith("Extensive and ancient feldspathic crust detected across north Hellas rim, Mars")
    bl = bib_entry("k", items[0]["csl"], flavor="biblatex")
    assert "journaltitle = {Remote Sensing}" in bl and "date = {2023-01-05}" in bl
    ascii_bib = bib_entry("k", items[0]["csl"], ascii_only=True)
    assert "Rivera-Valent{\\'\\i}n" in ascii_bib


def test_bib_import_keeps_keys_and_aliases(svc, tmp_path):
    _seed(svc)
    bib = tmp_path / "coauthor.bib"
    bib.write_text(r"""
@article{Phillips2022Geology,
  author = {Phillips, M. S. and Viviano, C. E.},
  title = {Extensive and ancient feldspathic crust detected across north {Hellas} rim, {Mars}},
  journal = {Geology}, year = {2022}, volume = {50}, pages = {1182--1186}, doi = {10.1130/G50341.1}}
@inproceedings{dapremont2017,
  author = {Dapremont, A. M. and Viviano-Beck, C. E.},
  title = {Investigation of {N}oachian crustal structure using {CRISM}},
  booktitle = {48th Lunar and Planetary Science Conference}, year = {2017}, pages = {1234}}
@article{depater2010,
  author = {Somebody, Else}, title = {A different work that reuses a key}, journal = {J}, year = {1999}}
""")
    rep = import_file(svc.lib, bib)
    assert rep["aliases_added"] == [{"alias": "Phillips2022Geology", "for": "phillipsExtensiveAncientFeldspathic2022"}]
    assert any(a["citekey"] == "dapremont2017" for a in rep["added"])
    assert rep["key_conflicts"] and rep["key_conflicts"][0]["file_key"] == "depater2010"
    assert svc.lib.get("Phillips2022Geology")["citekey"] == "phillipsExtensiveAncientFeldspathic2022"
    d = svc.lib.get("dapremont2017")["csl"]
    assert d["title"] == "Investigation of Noachian crustal structure using CRISM" and d["type"] == "paper-conference"


def test_ris_parse():
    recs = parse_ris("TY  - JOUR\nAU  - Domingue, D. L.\nAU  - Murchie, S. L.\nTI  - Mercury's global color mosaic\nJO  - Icarus\nPY  - 2015\nVL  - 257\nSP  - 477\nEP  - 488\nER  - \n")
    k, c, _ = recs[0]
    assert c["type"] == "article-journal" and c["page"] == "477-488" and c["container-title"] == "Icarus"
    assert c["author"][1] == {"family": "Murchie", "given": "S. L."}


def test_cite_string_parsing():
    g0 = parse_cite_string("[@smith2020 p. 4]")
    assert g0.refs[0].key == "smith2020" and g0.refs[0].locator == "4"
    g = parse_cite_string("[see @a, p. 33; -@b; @c, fig. 2 and table 1]")
    assert [(r.key, r.prefix, r.locator, r.label, r.suppress_author) for r in g.refs] == [
        ("a", "see", "33", "page", False), ("b", "", None, "page", True), ("c", "", "2", "figure", False)]
    assert g.refs[2].suffix.strip() == "and table 1"
    assert parse_cite_string("@key2020").narrative


def test_formatting_agu_and_disambiguation(svc):
    _seed(svc)
    r = svc.formatter.cite_strings(["[@phillipsSaltConstructsPaleoLake2023, p. 4]", "@phillips2023jezero",
                                    "[-@depater2010]", "[@phillipsExtensiveAncientFeldspathic2022; @depater2010]",
                                    "[@notInLibrary]"], "agu")
    texts = [c["text"] for c in r["citations"]]
    assert texts[0] == "(Phillips et al., 2023a, p. 4)"  # same authors+year: suffix by title order
    assert texts[1] == "Phillips et al. (2023b)"
    assert texts[2] == "(2010)"
    assert texts[3] == "(de Pater & Lissauer, 2010; Phillips et al., 2022)"
    assert texts[4].startswith("[MISSING")
    assert r["missing"] == ["notInLibrary"]
    bib = [e["text"] for e in r["bibliography"]]
    assert bib[0].startswith("de Pater, I., & Lissauer, J. J. (2010)")
    assert any("(2023b). Second Phillips et al. paper" in b for b in bib)
    assert any("(2023a). Salt Constructs" in b and "https://doi.org/10.3390/rs15020314" in b for b in bib)


def test_formatting_numeric_and_dependent_styles(svc):
    _seed(svc)
    r = svc.formatter.cite_strings(["[@phillipsExtensiveAncientFeldspathic2022]", "[@depater2010; @phillipsExtensiveAncientFeldspathic2022]"],
                                   "nature", output="runs")
    assert r["citations"][0]["text"] == "1" and r["citations"][0]["runs"][0].get("superscript")
    assert r["citations"][1]["text"] == "1,2"
    assert r["bibliography"][0]["text"].startswith("1. Phillips")
    icarus = svc.formatter.bibliography(["phillipsExtensiveAncientFeldspathic2022"], "icarus")
    assert icarus["style"]["title"] == "Icarus"
    assert icarus["bibliography"][0]["text"].startswith("Phillips, M.S., Viviano, C.E., Moersch, J.E., Rogers, A.D., McSween, H.Y., Seelos, F.P., 2022.")
    psj = svc.formatter.cite_strings(["[@phillipsExtensiveAncientFeldspathic2022]"], "psj")
    assert psj["citations"][0]["text"] == "(Phillips et al. 2022)"


def test_latex_check(svc, tmp_path):
    _seed(svc)
    svc.lib.add_alias("phillipsExtensiveAncientFeldspathic2022", "phillips2022geology")
    (tmp_path / "sections").mkdir()
    (tmp_path / "main.tex").write_text(r"""\documentclass{article}
\begin{document}
As shown by \citet{phillipsSaltConstructsPaleoLake2023} and \citep[p.~3]{phillips2022geology, depater2010}.
% \cite{commentedOut2020}
\input{sections/intro}
\bibliography{refs}
\end{document}
""")
    (tmp_path / "sections" / "intro.tex").write_text(r"Mars \parencite{phillipsSaltConstructsPaleoLake2O23} and \cites{depater2010}{phillips2023jezero}.")
    from rumil.latex import check_tex
    rep = check_tex(svc.lib, tmp_path / "main.tex", write_bib="refs.bib")
    assert rep["cited"] == 5
    assert [m["key"] for m in rep["missing"]] == ["phillipsSaltConstructsPaleoLake2O23"]
    assert rep["missing"][0]["suggestions"][0]["citekey"] == "phillipsSaltConstructsPaleoLake2023"
    assert rep["aliases"] == [{"key": "phillips2022geology", "library_key": "phillipsExtensiveAncientFeldspathic2022"}]
    out = (tmp_path / "refs.bib").read_text()
    assert "@article{phillips2022geology," in out and out.count("\n@") + out.startswith("@") == 4 or out.count("@") >= 4
    assert "commentedOut2020" not in str(rep)


def test_docx_render(svc, tmp_path):
    from docx import Document
    from rumil.docx_render import render_docx
    _seed(svc)
    d = Document()
    p = d.add_paragraph()
    p.add_run("Salt constructs are good targets [@phillips")  # marker split across runs
    p.add_run("SaltConstructsPaleoLake2023, p. 4]. ")
    p.add_run("@phillipsExtensiveAncientFeldspathic2022 found feldspar; email me@example.org; see [@depater2010; @missingKey].")
    d.add_paragraph("[@bibliography]")
    d.add_paragraph("Appendix text.")
    src = tmp_path / "draft.docx"
    d.save(src)
    rep = render_docx(svc.formatter, src, style="agu")
    assert rep["replaced"] == 2 and rep["references"] == 2 and rep["missing_keys"] == ["missingKey"]
    assert rep["left_unresolved"] == ["[@depater2010; @missingKey]"]
    out = Document(rep["output"])
    paras = [x.text for x in out.paragraphs]
    assert paras[0] == ("Salt constructs are good targets (Phillips et al., 2023, p. 4). Phillips et al. (2022) found feldspar; "
                        "email me@example.org; see [@depater2010; @missingKey].")
    hl = [r for r in out.paragraphs[0].runs if r.font.highlight_color is not None]
    assert hl and hl[0].text == "[@depater2010; @missingKey]"
    assert paras[1].startswith("Phillips, M. S., Viviano") and paras[2].startswith("Phillips, M. S., McInenly")
    assert paras[-1] == "Appendix text."
    assert Path(src).exists() and Document(src).paragraphs[1].text == "[@bibliography]"
    italic = [r.text for r in out.paragraphs[2].runs if r.font.italic]
    assert italic[:2] == ["Remote Sensing", "15"]
    rep2 = render_docx(svc.formatter, src, style="agu", out_path=tmp_path / "x.docx")
    assert rep2["output"].endswith("x.docx")
