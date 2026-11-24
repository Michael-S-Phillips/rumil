from rumil.citekeys import base_key
from rumil.csl import clean_item, initials_of, is_etal_name, parse_date, parse_name
from rumil.ids import detect, is_junk_url, normalize_arxiv, normalize_doi, normalize_isbn
from rumil.textutil import family_matches, title_similarity


def test_doi_normalization():
    assert normalize_doi("https://doi.org/10.1130/G50341.1") == "10.1130/G50341.1"
    assert normalize_doi("doi:10.3390/rs15020314.") == "10.3390/rs15020314"
    assert normalize_doi("https://dx.doi.org/10.1016%2Fj.icarus.2014.11.027") == "10.1016/j.icarus.2014.11.027"
    assert normalize_doi("(see 10.1016/S0019-1035(03)00123-4)") == "10.1016/S0019-1035(03)00123-4"
    assert normalize_doi("https://agupubs.onlinelibrary.wiley.com/doi/10.1029/2006JE002766/abstract") == "10.1029/2006JE002766"
    assert normalize_doi("not a doi") is None


def test_arxiv_and_isbn():
    assert normalize_arxiv("arXiv:2208.03533v2") == "2208.03533"
    assert normalize_arxiv("https://arxiv.org/pdf/2208.03533v1.pdf") == "2208.03533"
    assert normalize_arxiv("10.48550/arXiv.1501.01990") == "1501.01990"
    assert normalize_arxiv("hep-th/9901001") == "hep-th/9901001"
    assert normalize_isbn("0-521-85371-0") == "9780521853712"
    assert normalize_isbn("978-0-521-85371-2") == "9780521853712"
    assert normalize_isbn("978-0-521-85371-3") is None  # bad checksum


def test_detect():
    assert detect("10.1130/G50341.1").kind == "doi"
    assert detect("https://doi.org/10.1130/G50341.1").kind == "doi"
    assert detect("arXiv:2208.03533").kind == "arxiv"
    assert detect("https://arxiv.org/abs/2208.03533").value == "2208.03533"
    assert detect("2208.03533").kind == "arxiv"
    assert detect("ISBN 978-0-521-85371-2").kind == "isbn"
    assert detect("PMID: 12345").kind == "pmid"
    assert detect("2022Geo....50.1182P").kind == "bibcode"
    assert detect("https://www.mdpi.com/2072-4292/15/2/314").kind == "url"
    assert detect("https://pubs.geoscienceworld.org/doi/10.1130/G50341.1").kind == "doi"


def test_junk_urls():
    assert is_junk_url("<Go to ISI>://WOS:000358101300037")
    assert not is_junk_url("https://example.org/x")


def test_dates_and_names():
    assert parse_date("2023-01-05") == {"date-parts": [[2023, 1, 5]]}
    assert parse_date("2023/1") == {"date-parts": [[2023, 1]]}
    assert parse_date("March 2019") == {"date-parts": [[2019, 3]]}
    assert parse_date("5 January 2023") == {"date-parts": [[2023, 1, 5]]}
    assert parse_date("2025-04-29T21:32:55Z") == {"date-parts": [[2025, 4, 29]]}
    assert parse_date("in press") == {"literal": "in press"}
    assert parse_name("Phillips, Michael S.") == {"family": "Phillips", "given": "Michael S."}
    assert parse_name("Imke de Pater") == {"family": "de Pater", "given": "Imke"}
    assert parse_name("{NASA Ames}") == {"literal": "NASA Ames"}
    assert initials_of("Michael S.") == "M. S."
    assert initials_of("Jean-Pierre") == "J.-P."
    assert is_etal_name({"family": "al", "given": "et"}) and is_etal_name({"literal": "others"})


def test_clean_item():
    c = clean_item({"type": "journal-article", "title": "A title.", "DOI": "https://doi.org/10.1000/X", "page": "37–72",
                    "issued": "2020", "volume": 12, "author": ["Turing, Alan", ""]})
    assert c["type"] == "article-journal" and c["title"] == "A title" and c["DOI"] == "10.1000/X"
    assert c["page"] == "37-72" and c["issued"] == {"date-parts": [[2020]]} and c["volume"] == "12"
    assert c["author"] == [{"family": "Turing", "given": "Alan"}]
    assert clean_item({"title": "x", "author": [{"family": "Phillips", "given": "MS"}, {"family": "Moersch", "given": "J.E."}]})["author"] == [
        {"family": "Phillips", "given": "M. S."}, {"family": "Moersch", "given": "J. E."}]


def test_citekeys_match_zotero_format():
    csl = {"title": "Salt Constructs in Paleo-Lake Basins as High-Priority Astrobiology Targets",
           "author": [{"family": "Phillips", "given": "M."}], "issued": {"date-parts": [[2023]]}}
    assert base_key(csl) == "phillipsSaltConstructsPaleoLake2023"
    csl2 = {"title": "Stable localized moving patterns in the 2-D Gray-Scott model", "author": [{"family": "Munafo"}],
            "issued": {"date-parts": [[2014]]}}
    assert base_key(csl2) == "munafoStableLocalizedMoving2014"
    csl3 = {"title": "Ångström-scale structure of the Rivera-Valentín crust", "author": [{"family": "Rivera-Valentín"}],
            "issued": {"date-parts": [[2020]]}}
    assert base_key(csl3) == "riveravalentinAngstromscaleStructureRiveraValentin2020"


def test_title_similarity():
    a = "Mercury's global color mosaic: An update from MESSENGER's orbital observations"
    b = "Mercury’s global color mosaic: an update from MESSENGER’s orbital observations"
    assert title_similarity(a, b) == 1.0
    assert title_similarity("Mercury's global color mosaic", a) >= 0.9  # missing subtitle
    assert title_similarity("RETRACTED: Myc is a prognostic biomarker", "Myc is a prognostic biomarker") == 1.0
    assert title_similarity(a, "Global major-element maps of Mercury") < 0.6
    assert family_matches("Rivera-Valentín", "Rivera-Valentin")
    assert family_matches("de Pater", "Pater")
    assert not family_matches("Phillips", "Philips-Smith")
