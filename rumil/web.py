"""The library window: a local web app (http://127.0.0.1:23120) plus the endpoint the
"Save to Rumil" browser extension talks to. Listens on localhost only and rejects
requests from other web sites.
"""

from __future__ import annotations

import json
import re
import threading
import webbrowser
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__
from .csl import authors_short, clean_item, year_of
from .db import NotFound
from .http import HttpError
from .ids import detect, normalize_arxiv, normalize_doi
from .resolvers import NotFoundError, PageMeta
from .textutil import strip_markup
from .verify import describe_status

STATIC = Path(__file__).parent / "static"
_svc = None
_svc_lock = threading.Lock()


def svc():
    global _svc
    with _svc_lock:
        if _svc is None:
            from .service import Rumil
            _svc = Rumil()
        return _svc


# ------------------------------------------------------------------ security
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


class LocalOnly:
    """Reject DNS-rebinding (unexpected Host), requests from other web sites (foreign Origin), and
    form-encoded cross-site posts. Only the library window itself and the bundled extension may call the API."""

    def __init__(self, app, port: int, extension_ids=()):
        self.app = app
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        self.extensions = {f"chrome-extension://{e}" for e in extension_ids if e}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        host = headers.get("host", "")
        origin = headers.get("origin", "")
        ext = origin in self.extensions
        if host not in self.hosts:
            return await PlainTextResponse("Forbidden host", status_code=403)(scope, receive, send)
        if origin and origin not in self.origins and not ext:
            return await PlainTextResponse("Forbidden origin", status_code=403)(scope, receive, send)
        if scope["method"] == "OPTIONS":
            resp = Response(status_code=204)
            if ext:
                resp.headers["Access-Control-Allow-Origin"] = origin
                resp.headers["Access-Control-Allow-Methods"] = "GET, POST"
                resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            return await resp(scope, receive, send)
        ctype = headers.get("content-type", "").split(";")[0].strip().lower()
        if scope["method"] not in ("GET", "HEAD") and scope["path"].startswith("/api/") and ctype != "application/json":
            return await PlainTextResponse("JSON body required", status_code=415)(scope, receive, send)
        if ext and not (scope["path"].startswith("/api/capture") or scope["path"] in ("/api/ping", "/api/collections")):
            return await PlainTextResponse("The extension may only save references", status_code=403)(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                hdrs = message.setdefault("headers", [])
                names = {k.lower() for k, _ in hdrs}
                if b"content-security-policy" not in names:
                    hdrs.append((b"content-security-policy", CSP.encode()))
                hdrs.append((b"x-content-type-options", b"nosniff"))
                hdrs.append((b"referrer-policy", b"no-referrer"))
                if ext:
                    hdrs.append((b"access-control-allow-origin", origin.encode()))
            await send(message)

        return await self.app(scope, receive, send_wrapper)


def _err(e: Exception, status: int = 400) -> JSONResponse:
    if isinstance(e, (NotFound, NotFoundError, FileNotFoundError)):
        status = 404
    elif isinstance(e, HttpError):
        status = 502
    return JSONResponse({"error": str(e).strip("'\"")}, status_code=status)


def api(fn):
    """Wrap a sync handler: JSON in, JSON out, errors as JSON."""
    def handler(request: Request):
        try:
            body = None
            if request.method in ("POST", "PATCH", "PUT", "DELETE"):
                raw = _read_body(request)
                body = json.loads(raw) if raw else {}
            res = fn(request, body)
            if isinstance(res, Response):
                return res
            return JSONResponse(res)
        except Exception as e:  # noqa: BLE001 - surface every error to the UI
            return _err(e)
    handler.__name__ = fn.__name__
    return handler


def _read_body(request: Request) -> bytes:
    import anyio
    return anyio.from_thread.run(request.body)


def _row(it: dict) -> dict:
    c = it["csl"]
    return {"id": it["id"], "citekey": it["citekey"], "type": c.get("type"), "title": strip_markup(c.get("title", "")),
            "creator": authors_short(c), "year": year_of(c), "container": c.get("container-title") or c.get("publisher") or "",
            "doi": c.get("DOI"), "status": it["verify_status"], "date_added": it["date_added"], "deleted": it["deleted"]}


# ------------------------------------------------------------------- routes
@api
def state(request, body):
    s = svc()
    from .config import setup_warnings
    return {"version": __version__, "stats": s.lib.stats(), "collections": s.lib.collections_tree(),
            "setup_warnings": setup_warnings(s.cfg),
            "tags": s.lib.all_tags(), "default_style": s.cfg.get("default_style"),
            "settings": {"contact_email": s.cfg.get("contact_email"), "ads_token_set": bool(s.cfg.get("ads_token")),
                         "zotero_dir": str(s.cfg.zotero_dir), "home": str(s.cfg.home)},
            "jobs": s.jobs.all()[:5]}


@api
def list_items(request, body):
    s = svc()
    p = request.query_params
    items, total = s.lib.search(p.get("q", ""), collection=p.get("collection") or None, tag=p.get("tag") or None,
                                status=p.get("status") or None, special=p.get("special") or None,
                                sort=p.get("sort", "relevance" if p.get("q") else "date_added"), order=p.get("order", "desc"),
                                limit=min(int(p.get("limit", 200)), 1000), offset=int(p.get("offset", 0)))
    return {"total": total, "items": [_row(i) for i in items]}


def _detail(s, iid) -> dict:
    it = s.lib.get(iid, include_deleted=True)
    if not it:
        raise NotFound(f"No item {iid}")
    preview = None
    try:
        r = s.formatter.bibliography([it["citekey"]], None, output="html")
        preview = {"html": r["bibliography"][0]["html"] if r["bibliography"] else "", "style": r["style"]["title"]}
        c = s.formatter.cite_strings([f"[@{it['citekey']}]", f"@{it['citekey']}"], None, bibliography=False)
        preview["parenthetical"] = c["citations"][0]["text"]
        preview["narrative"] = c["citations"][1]["text"]
    except Exception as e:  # style problems shouldn't hide the item
        preview = {"error": str(e)}
    it["status_meaning"] = describe_status(it["verify_status"])
    it["preview"] = preview
    it["history"] = s.lib.history(iid, limit=25)
    return it


@api
def get_item(request, body):
    return _detail(svc(), int(request.path_params["id"]))


@api
def add_item(request, body):
    s = svc()
    r = s.add(body.get("identifier", ""), collection=body.get("collection") or None, tags=body.get("tags") or [])
    return r


@api
def add_manual(request, body):
    s = svc()
    return s.add_manual(body.get("csl") or {}, evidence_url=body.get("evidence_url") or None,
                        collection=body.get("collection") or None)


@api
def patch_item(request, body):
    s = svc()
    iid = int(request.path_params["id"])
    if "csl" in body:
        s.lib.update_item(iid, csl=body["csl"], reason="edited in library window")
    if "patch" in body:
        s.lib.update_item(iid, patch=body["patch"], reason="edited in library window")
    if body.get("citekey"):
        s.lib.set_citekey(iid, body["citekey"])
    if "tags" in body:
        s.lib.set_tags(iid, body["tags"])
    s.changed()
    return _detail(s, iid)


@api
def delete_item(request, body):
    s = svc()
    s.lib.trash(int(request.path_params["id"]))
    s.changed()
    return {"ok": True}


@api
def restore_item(request, body):
    s = svc()
    s.lib.restore(int(request.path_params["id"]))
    s.changed()
    return {"ok": True}


@api
def empty_trash(request, body):
    s = svc()
    n = s.lib.empty_trash()
    s.changed()
    return {"purged": n}


@api
def verify_item(request, body):
    s = svc()
    s.verifier.verify_and_store(int(request.path_params["id"]), apply_safe=bool(body.get("apply_safe")))
    s.changed()
    return _detail(s, int(request.path_params["id"]))


@api
def apply_item(request, body):
    s = svc()
    s.verifier.apply(int(request.path_params["id"]), body.get("ids") or "safe")
    s.changed()
    return _detail(s, int(request.path_params["id"]))


@api
def keep_item(request, body):
    s = svc()
    s.verifier.accept(int(request.path_params["id"]), body.get("fields") or [])
    s.changed()
    return _detail(s, int(request.path_params["id"]))


@api
def verify_many(request, body):
    s = svc()
    if body.get("ids"):
        refs = [int(i) for i in body["ids"]]
        return s.verify(refs, apply_safe=_apply_mode(body), background_over=0)
    return s.verify(None, collection=body.get("collection") or None, everything=not body.get("collection"),
                    only_unchecked=bool(body.get("only_unchecked")), apply_safe=_apply_mode(body),
                    background_over=0)


def _apply_mode(body) -> bool | str:
    v = body.get("apply_safe")
    return "corrections" if v == "corrections" else bool(v)


@api
def job(request, body):
    j = svc().jobs.get(request.path_params["id"])
    if not j:
        raise NotFound("No such job")
    if request.method == "DELETE":
        j.cancel_requested = True
    return j.to_dict()


@api
def add_note(request, body):
    s = svc()
    nid = s.lib.add_note(int(request.path_params["id"]), body.get("html", ""))
    return {"id": nid}


@api
def note(request, body):
    s = svc()
    nid = int(request.path_params["id"])
    if request.method == "DELETE":
        s.lib.delete_note(nid)
    else:
        s.lib.update_note(nid, body.get("html", ""))
    return {"ok": True}


def attachment_file(request: Request):
    s = svc()
    a = s.lib.attachment(int(request.path_params["id"]))
    if not a or not a.get("path") or not Path(a["path"]).is_file():
        return JSONResponse({"error": "File not found on disk"}, status_code=404)
    path = Path(a["path"])
    with path.open("rb") as fh:
        is_pdf = fh.read(5) == b"%PDF-"
    if is_pdf:  # Chrome's PDF viewer runs isolated from this page
        return FileResponse(path, media_type="application/pdf",
                            headers={"Content-Disposition": f"inline; filename=\"{path.name}\""})
    # Anything else (e.g. saved HTML snapshots) is downloaded, never rendered on this origin.
    return FileResponse(path, media_type="application/octet-stream",
                        headers={"Content-Disposition": f"attachment; filename=\"{path.name}\"",
                                 "Content-Security-Policy": "sandbox"})


@api
def collections(request, body):
    s = svc()
    if request.method == "POST":
        out = s.lib.create_collection(body["name"], body.get("parent"))
        s.changed()
        return out
    return {"collections": s.lib.collections_tree()}


@api
def collection(request, body):
    s = svc()
    cid = int(request.path_params["id"])
    if request.method == "DELETE":
        s.lib.delete_collection(cid)
    else:
        if body.get("name"):
            s.lib.rename_collection(cid, body["name"])
        if "parent" in body:
            s.lib.move_collection(cid, body["parent"])
    s.changed()
    return {"collections": s.lib.collections_tree()}


@api
def collection_items(request, body):
    s = svc()
    cid = int(request.path_params["id"])
    ids = [int(i) for i in body.get("ids", [])]
    n = s.lib.remove_from_collection(cid, ids) if request.method == "DELETE" else s.lib.add_to_collection(cid, ids, create=False)
    s.changed()
    return {"changed": n}


@api
def tags(request, body):
    s = svc()
    if request.method == "POST":
        s.lib.add_tags([int(i) for i in body.get("ids", [])], body.get("tags", []))
        s.changed()
    return {"tags": s.lib.all_tags()}


@api
def import_zotero(request, body):
    s = svc()
    from .zotero import import_zotero as run

    def go(job):
        rep = run(s.lib, progress=lambda stage, n, t: job.update(n, t, stage))
        s.changed()
        keep = ("summary", "added", "updated", "unchanged", "conflicts", "key_conflicts", "zotero_citekey_changed",
                "merged_identical", "deleted_in_zotero", "errors")
        return {k: (v[:50] if isinstance(v, list) else v) for k, v in rep.items() if k in keep}

    return s.jobs.start("Sync from Zotero", go).to_dict()


@api
def import_file(request, body):
    s = svc()
    from .importers import import_records, parse_bibtex, parse_csljson, parse_ris
    name = (body.get("filename") or "").lower()
    text = body.get("text") or ""
    if name.endswith(".ris") or re.search(r"^TY  - ", text, re.M):
        recs = parse_ris(text)
    elif name.endswith(".json") or text.lstrip().startswith(("[", "{")):
        recs = parse_csljson(text)
    else:
        recs = parse_bibtex(text)
    rep = import_records(s.lib, recs, collection=body.get("collection") or None, source=Path(name).suffix.lstrip(".") or "file")
    s.changed()
    return rep


def export(request: Request):
    s = svc()
    p = request.query_params
    fmt = p.get("format", "bibtex")
    ids = [int(i) for i in p.get("ids", "").split(",") if i.strip().isdigit()]
    keys = [s.lib.get(i, full=False)["citekey"] for i in ids] if ids else None
    try:
        out = s.export(fmt, keys, p.get("collection") or None)
    except Exception as e:
        return _err(e)
    ext = {"bibtex": "bib", "biblatex": "bib", "csljson": "json", "ris": "ris"}.get(fmt, "txt")
    return Response(out["text"], media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=\"references.{ext}\""})


@api
def format_refs(request, body):
    s = svc()
    keys = body.get("keys") or [s.lib.get(int(i), full=False)["citekey"] for i in body.get("ids", [])]
    r = s.formatter.render([], body.get("style") or None, output="html", extra_bib_keys=keys)
    cites = s.formatter.cite_strings(["[" + "; ".join("@" + k for k in keys) + "]"], body.get("style") or None,
                                     bibliography=False, output="html") if keys else {"citations": []}
    r["citation"] = cites["citations"][0] if cites["citations"] else None
    return r


@api
def styles(request, body):
    s = svc()
    if request.method == "POST":
        p = s.styles.install(body["name"])
        return {"installed": p.stem, "styles": s.styles.list()}
    return {"styles": s.styles.list(), "default": s.cfg.get("default_style")}


@api
def settings(request, body):
    s = svc()
    for k in ("default_style", "contact_email", "ads_token", "zotero_dir"):
        if k in body and body[k] is not None:
            s.cfg.set(k, body[k].strip() if isinstance(body[k], str) else body[k])
    s.cfg.save()
    s.http.set_contact_email(s.cfg.get("contact_email", ""))
    s.resolver.ads_token = s.cfg.get("ads_token", "")
    return {"ok": True}


@api
def duplicates(request, body):
    s = svc()
    return {"groups": [[_row(i) for i in g] for g in s.lib.duplicate_groups()]}


@api
def merge(request, body):
    s = svc()
    r = s.lib.merge(int(body["keep"]), [int(i) for i in body.get("remove", [])])
    s.changed()
    return _detail(s, r["id"])


# --------------------------------------------------------------- capture
def _capture_meta(body) -> tuple[str, PageMeta]:
    url = body.get("url") or ""
    meta = {k.lower(): (v if isinstance(v, list) else [v]) for k, v in (body.get("meta") or {}).items() if v}
    return url, PageMeta(meta, body.get("title") or "", body.get("jsonld") or [], url)


def _capture_identifier(url: str, pm: PageMeta) -> str | None:
    d = pm.doi()
    if not d and "/10." in url:
        d = normalize_doi(url)
    if d:
        return d
    aid = pm.first("citation_arxiv_id") or (normalize_arxiv(url) if "arxiv.org" in url else None)
    if aid:
        return f"arXiv:{normalize_arxiv(aid) or aid}"
    ident = detect(url)
    if ident and ident.kind in ("doi", "arxiv", "pmid", "bibcode"):
        return ident.value if ident.kind != "arxiv" else f"arXiv:{ident.value}"
    return None


def _capture_target(s, url: str, pm: PageMeta) -> tuple[str, object, list[str]]:
    """Work out what to save: (identifier or URL, Resolution or None, warnings)."""
    from .textutil import title_similarity
    page_title = strip_markup(pm.title())
    ident = _capture_identifier(url, pm)
    warnings: list[str] = []
    if not ident and pm.first("rumil_doi_link"):
        cand = normalize_doi(pm.first("rumil_doi_link"))
        if cand:
            try:
                reg = s.resolver.resolve_doi(cand)
                if page_title and title_similarity(page_title, reg.csl.get("title")) >= 0.8:
                    return cand, reg, warnings
            except (NotFoundError, HttpError):
                pass
    try:
        reg = s.resolver.resolve(ident or url)
    except (NotFoundError, HttpError) as e:
        return ident or url, None, [f"Rumil could not look this up ({e}); using the metadata your browser saw"]
    if page_title and reg.authoritative and title_similarity(page_title, reg.csl.get("title")) < 0.5 \
            and not page_title.lower().startswith(("pdf", "http")):
        warnings.append(f"This page's title ('{page_title[:70]}') doesn't match the registry record; check it's the right work")
    return ident or url, reg, warnings + list(reg.warnings)


@api
def capture_preview(request, body):
    s = svc()
    url, pm = _capture_meta(body)
    target, reg, warnings = _capture_target(s, url, pm)
    if reg is not None:
        csl, source, authoritative = reg.csl, reg.source, reg.authoritative
    else:
        csl, source, authoritative = pm.to_csl(url), "web", False
    dups = s.lib.find_duplicates_of(csl, strong_only=True)
    return {"csl": csl, "source": source, "verified": authoritative, "warnings": warnings,
            "duplicate": {"citekey": dups[0][0]["citekey"], "why": dups[0][1]} if dups else None,
            "collections": s.lib.collections_tree(), "summary": {
                "title": strip_markup(csl.get("title", "")), "creator": authors_short(csl), "year": year_of(csl),
                "container": csl.get("container-title") or csl.get("publisher"), "doi": csl.get("DOI"), "type": csl.get("type")}}


@api
def capture(request, body):
    s = svc()
    url, pm = _capture_meta(body)
    coll = body.get("collection") or None
    tags_ = body.get("tags") or []
    target, reg, warnings = _capture_target(s, url, pm)
    if reg is not None:
        out = s.add(target, collection=coll, tags=tags_)
        out["warnings"] = list(dict.fromkeys((out.get("warnings") or []) + warnings))
        pdf = pm.first("citation_pdf_url")
        if out["status"] == "added" and pdf and not out.get("pdf"):
            item = s.lib.get(out["item"]["citekey"])
            if item and not item.get("attachments"):
                out["pdf"] = s.attach_pdf_from_url(item["id"], pdf)
        return out
    csl = clean_item(pm.to_csl(url))
    dups = s.lib.find_duplicates_of(csl, strong_only=True)
    if dups:
        return {"status": "exists", "item": s._item_out(s.lib.get(dups[0][0]["id"]))}
    item = s.lib.add_item(csl, source="web", collections=[coll] if coll else [], tags=tags_)
    s.lib.set_verification(item["id"], "unverified",
                           {"summary": "Saved from the page's own metadata; no DOI or registry record confirms it"}, [])
    pdf = pm.first("citation_pdf_url")
    if pdf:
        s.lib.add_attachment(item["id"], title="PDF (publisher link)", url=pdf, content_type="application/pdf",
                             link_mode="linked_url")
    s.changed()
    return {"status": "added", "item": s._item_out(s.lib.get(item["id"])), "source": "web",
            "warnings": warnings + ["Saved from page metadata only (unverified)"]}


def index(request: Request):
    # Stamp the stylesheet and script URLs with their modification times so a browser never keeps using an old copy
    # after an update.
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("app.css", "app.js"):
        html = html.replace(f"/static/{name}\"", f"/static/{name}?v={int((STATIC / name).stat().st_mtime)}\"")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def ping(request: Request):
    return JSONResponse({"app": "rumil", "version": __version__})


def build_app(port: int) -> Starlette:
    routes = [
        Route("/", index),
        Route("/api/ping", ping),
        Route("/api/state", state),
        Route("/api/items", list_items, methods=["GET"]),
        Route("/api/items", add_item, methods=["POST"]),
        Route("/api/items/manual", add_manual, methods=["POST"]),
        Route("/api/items/{id:int}", get_item, methods=["GET"]),
        Route("/api/items/{id:int}", patch_item, methods=["PATCH"]),
        Route("/api/items/{id:int}", delete_item, methods=["DELETE"]),
        Route("/api/items/{id:int}/restore", restore_item, methods=["POST"]),
        Route("/api/items/{id:int}/verify", verify_item, methods=["POST"]),
        Route("/api/items/{id:int}/apply", apply_item, methods=["POST"]),
        Route("/api/items/{id:int}/keep", keep_item, methods=["POST"]),
        Route("/api/items/{id:int}/notes", add_note, methods=["POST"]),
        Route("/api/notes/{id:int}", note, methods=["PATCH", "DELETE"]),
        Route("/api/attachments/{id:int}/file", attachment_file),
        Route("/api/trash/empty", empty_trash, methods=["POST"]),
        Route("/api/verify", verify_many, methods=["POST"]),
        Route("/api/jobs/{id}", job, methods=["GET", "DELETE"]),
        Route("/api/collections", collections, methods=["GET", "POST"]),
        Route("/api/collections/{id:int}", collection, methods=["PATCH", "DELETE"]),
        Route("/api/collections/{id:int}/items", collection_items, methods=["POST", "DELETE"]),
        Route("/api/tags", tags, methods=["GET", "POST"]),
        Route("/api/import/zotero", import_zotero, methods=["POST"]),
        Route("/api/import/file", import_file, methods=["POST"]),
        Route("/api/export", export),
        Route("/api/format", format_refs, methods=["POST"]),
        Route("/api/styles", styles, methods=["GET", "POST"]),
        Route("/api/settings", settings, methods=["POST"]),
        Route("/api/duplicates", duplicates),
        Route("/api/merge", merge, methods=["POST"]),
        Route("/api/capture/preview", capture_preview, methods=["POST"]),
        Route("/api/capture", capture, methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(STATIC)), name="static"),
    ]
    app = Starlette(routes=routes)
    from .config import Config
    return LocalOnly(app, port, extension_ids=Config().get("allowed_extensions") or [])


def serve(port: int | None = None, open_browser: bool = False) -> None:
    import socket

    import uvicorn
    from .config import Config

    port = int(port or Config().get("web_port", 23120))
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            print(f"Something is already running on port {port} (probably Rumil). Open http://127.0.0.1:{port}")
            if open_browser:
                webbrowser.open(f"http://127.0.0.1:{port}")
            return
    svc()  # open the library before accepting requests
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"Rumil library window: http://127.0.0.1:{port}")
    uvicorn.run(build_app(port), host="127.0.0.1", port=port, log_level="warning")
