import os
import sys

import anyio
import pytest



EXT = "chrome-extension://melhjdcjdcnjigajdciofmkijnehmcjm"


@pytest.fixture
def web(svc, monkeypatch):
    from starlette.testclient import TestClient
    import rumil.web as web
    monkeypatch.setattr(web, "_svc", svc)
    return TestClient(web.build_app(23120), base_url="http://127.0.0.1:23120")


def test_web_api_flow(web, svc):
    r = web.post("/api/items", json={"identifier": "10.1130/G50341.1", "collection": "Hellas"})
    assert r.status_code == 200, r.text
    key = r.json()["item"]["citekey"]
    st = web.get("/api/state").json()
    assert st["stats"]["items"] == 1 and st["collections"][0]["name"] == "Hellas"
    items = web.get("/api/items", params={"q": "feldspathic"}).json()
    assert items["total"] == 1
    iid = items["items"][0]["id"]
    d = web.get(f"/api/items/{iid}").json()
    assert d["citekey"] == key and d["preview"]["parenthetical"].startswith("(Phillips et al.")
    d2 = web.patch(f"/api/items/{iid}", json={"patch": {"volume": "51"}, "tags": ["feldspar"]}).json()
    assert d2["csl"]["volume"] == "51" and d2["tags"] == ["feldspar"] and d2["verify_status"] == "unchecked"
    v = web.post(f"/api/items/{iid}/verify", json={}).json()
    assert v["verify_status"] == "differs"  # same work, but the volume we typed disagrees with Crossref
    fix = next(s for s in v["suggestions"] if s.get("field") == "volume")
    assert fix["proposed"] == "50" and fix["correction"] and not fix["safe"]
    v2 = web.post(f"/api/items/{iid}/apply", json={"ids": "corrections"}).json()
    assert v2["verify_status"] == "verified" and v2["csl"]["volume"] == "50"
    bib = web.get("/api/export", params={"format": "bibtex", "ids": str(iid)})
    assert bib.status_code == 200 and f"@article{{{key}," in bib.text
    fm = web.post("/api/format", json={"ids": [iid], "style": "icarus"}).json()
    assert fm["bibliography"][0]["text"].startswith("Phillips, M.S.")


def test_web_rejects_foreign_sites(web):
    assert web.post("/api/items", json={"identifier": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert web.get("/api/state", headers={"Host": "evil.example:23120"}).status_code == 403
    assert web.post("/api/items", data="identifier=x", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert web.get("/api/state", headers={"Origin": "chrome-extension://abcdef"}).status_code == 403  # unknown extension
    assert web.get("/api/state", headers={"Origin": EXT}).status_code == 403  # the extension may only save
    ok = web.post("/api/capture/preview", json={"url": "https://water.lsbu.ac.uk/x", "meta": {}}, headers={"Origin": EXT})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == EXT
    assert "script-src 'self'" in web.get("/").headers["content-security-policy"]


def test_capture_endpoint(web):
    meta = {"citation_title": "Salt Constructs in Paleo-Lake Basins as High-Priority Astrobiology Targets",
            "citation_doi": "10.3390/rs15020314", "citation_author": ["Phillips, Michael S."]}
    pv = web.post("/api/capture/preview", json={"url": "https://www.mdpi.com/2072-4292/15/2/314", "meta": meta},
                  headers={"Origin": EXT}).json()
    assert pv["verified"] and pv["summary"]["doi"] == "10.3390/rs15020314" and pv["duplicate"] is None
    saved = web.post("/api/capture", json={"url": "https://www.mdpi.com/2072-4292/15/2/314", "meta": meta, "collection": "Salts"},
                     headers={"Origin": EXT}).json()
    assert saved["status"] == "added" and saved["item"]["status"] == "verified"
    # A paywalled page Rumil can't fetch: falls back to what the browser saw, marked unverified.
    meta2 = {"citation_title": "A paper behind a paywall", "citation_author": ["Doe, Jane"], "citation_journal_title": "Journal X",
             "citation_publication_date": "2021"}
    saved2 = web.post("/api/capture", json={"url": "https://paywalled.example.com/article/1", "meta": meta2},
                      headers={"Origin": EXT}).json()
    assert saved2["status"] == "added" and saved2["item"]["status"] == "unverified"


def test_mcp_tools_in_process(svc, monkeypatch):
    import rumil.mcp_server as m
    monkeypatch.setattr(m, "_svc", svc)
    added = m.add_reference("10.3390/rs15020314", collection="Salts", tags=["salts"])
    key = added["item"]["citekey"]
    assert m.search_library("salt paleo")["items"][0]["citekey"] == key
    ref = m.get_reference(key, include_bibtex=True)
    assert ref["status"] == "verified" and ref["bibtex"].startswith("@article{" + key)
    found = m.find_reference(title="Mercury's global color mosaic: An update from MESSENGER's orbital observations", author="Domingue", year=2015)
    assert found["candidates"][0]["confidence"] == "high"
    f = m.format_citations([f"[@{key}]", "[@doesNotExist2020]"], style="grl")
    assert f["citations"][0]["text"] == "(Phillips et al., 2023)" and f["missing"] == ["doesNotExist2020"]
    assert m.manage_collections("list")["collections"][0]["name"] == "Salts"
    assert m.library_status()["items"] == 1


def test_mcp_stdio_protocol(tmp_path):
    """Start the real server over stdio and talk MCP to it."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from rumil.config import Config
    from rumil.db import Library

    home = tmp_path / "R"
    lib = Library(Config(home))
    lib.add_item({"type": "article-journal", "title": "Extensive and ancient feldspathic crust detected across north Hellas rim, Mars",
                  "author": [{"family": "Phillips", "given": "Michael S."}], "issued": "2022", "container-title": "Geology",
                  "DOI": "10.1130/G50341.1"}, source="test")
    lib.close()
    env = dict(os.environ, RUMIL_HOME=str(home))
    params = StdioServerParameters(command=sys.executable, args=["-m", "rumil.mcp_server"], env=env)

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = {t.name for t in (await s.list_tools()).tools}
                res = await s.call_tool("search_library", {"query": "feldspathic hellas"})
                text = "".join(getattr(c, "text", "") for c in res.content)
                res2 = await s.call_tool("format_citations", {"citations": ["@phillipsExtensiveAncientFeldspathic2022"], "style": "agu"})
                text2 = "".join(getattr(c, "text", "") for c in res2.content)
                return tools, text, text2

    tools, text, text2 = anyio.run(go)
    for t in ("search_library", "add_reference", "find_reference", "verify_references", "format_citations",
              "check_latex", "format_word_document", "read_reference_fulltext", "sync_zotero"):
        assert t in tools
    assert "phillipsExtensiveAncientFeldspathic2022" in text
    assert "Phillips (2022)" in text2


def test_library_window_panes_scroll_and_assets_are_not_cached_stale(web):
    """The panes need a bounded row to scroll within; and after an update the browser must load the new CSS/JS."""
    page = web.get("/").text
    assert "/static/app.css?v=" in page and "/static/app.js?v=" in page
    css = web.get("/static/app.css").text
    assert "grid-template-rows: minmax(0, 1fr)" in css and ".layout > * { min-height: 0; }" in css
    js = web.get("/static/app.js").text
    assert "d-close" in js  # narrow windows: the details overlay can be closed
