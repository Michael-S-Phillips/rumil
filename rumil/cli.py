"""Command-line interface: `rumil <command>`. Run `rumil -h` for the list."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .config import DEFAULTS, SECRET_KEYS, Config, rumil_home, setup_warnings


def _svc():
    from .service import Rumil
    return Rumil()


def _print(obj) -> None:
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    else:
        print(obj)


# ------------------------------------------------------------------ commands
def cmd_init(a) -> None:
    cfg = Config()
    cfg.home.mkdir(parents=True, exist_ok=True)
    if a.zotero_dir:
        cfg.set("zotero_dir", a.zotero_dir)
    if a.email is not None:
        cfg.set("contact_email", a.email)
    if a.ads_token is not None:
        cfg.set("ads_token", a.ads_token)
    if a.style:
        cfg.set("default_style", a.style)
    cfg.save()
    cfg.styles_dir.mkdir(exist_ok=True)
    zstyles = cfg.zotero_dir / "styles"
    copied = 0
    if zstyles.exists():
        for p in zstyles.glob("*.csl"):
            dest = cfg.styles_dir / p.name
            if not dest.exists():
                shutil.copy2(p, dest)
                copied += 1
    print(f"Rumil folder: {cfg.home}")
    print(f"Config:       {cfg.path}")
    print(f"Styles:       copied {copied} CSL styles from {zstyles}" if zstyles.exists() else "Styles:       (no Zotero styles folder found)")
    from .db import Library
    Library(cfg).close()
    print("Library database ready.")


def cmd_import_zotero(a) -> None:
    s = _svc()
    from .zotero import import_zotero

    def prog(stage, n, total):
        print(f"\r  {stage}: {n}/{total}", end="", file=sys.stderr, flush=True)

    try:
        rep = import_zotero(s.lib, a.zotero_dir, dry_run=a.dry_run, progress=prog)
    except FileNotFoundError as e:
        sys.exit(f"{e}. If your Zotero data folder is somewhere else, run: rumil config set zotero_dir /path/to/Zotero")
    print(file=sys.stderr)
    s.flush()
    if a.json:
        _print(rep)
        return
    print(rep.get("summary") or rep)
    if rep.get("merged_identical"):
        print(f"  {len(rep['merged_identical'])} Zotero items shared a DOI with another item and were merged (their keys kept as aliases).")
    if rep.get("conflicts"):
        print(f"  {len(rep['conflicts'])} conflicts kept the Rumil version.")
    if rep.get("errors"):
        print(f"  {len(rep['errors'])} items could not be imported: {rep['errors'][:3]}")
    print(f"  library.bib written to {s.cfg.resolve(s.cfg.get('auto_export', {}).get('bibtex'))}")


def cmd_import(a) -> None:
    s = _svc()
    if a.dry_run:
        from .importers import import_file
        rep = import_file(s.lib, a.file, collection=a.collection, dry_run=True)
    else:
        rep = s.import_bibfile(a.file, collection=a.collection, verify=False)
        if not a.no_verify and rep["added"]:
            keys = [x["citekey"] for x in rep["added"]]
            print(f"Verifying {len(keys)} new entries...", file=sys.stderr)
            res = s.verifier.verify_many(keys, workers=int(s.cfg.get("verify_concurrency", 3)))
            rep["verification"] = res
    s.flush()
    if a.json:
        _print(rep)
        return
    print(rep["summary"])
    if rep.get("verification"):
        print("  verification: " + ", ".join(f"{v} {k}" for k, v in rep["verification"]["by_status"].items()))
    for k in rep.get("key_conflicts", []):
        print(f"  ! {k['message']}")


def cmd_add(a) -> None:
    s = _svc()
    for ident in a.identifiers:
        try:
            r = s.add(ident, collection=a.collection, tags=a.tag or [])
            it = r["item"]
            print(f"{r['status']:>7}  @{it['citekey']}  [{it['status']}]  {it['title'][:80]}")
            for w in r.get("warnings") or []:
                print(f"         ! {w}")
        except Exception as e:
            print(f"  error  {ident}: {e}")
    s.flush()


def cmd_search(a) -> None:
    s = _svc()
    items, total = s.lib.search(" ".join(a.query), limit=a.limit, collection=a.collection, status=a.status)
    for it in items:
        c = it["csl"]
        from .csl import authors_short, year_of
        print(f"@{it['citekey']:<45} {it['verify_status']:<15} {authors_short(c)[:25]:<25} {year_of(c) or '':<5} {c.get('title', '')[:70]}")
    print(f"({len(items)} of {total})")


def cmd_show(a) -> None:
    s = _svc()
    _print(s.item_detail(a.citekey))


def cmd_verify(a) -> None:
    s = _svc()
    for w in setup_warnings(s.cfg):
        print(f"! {w}", file=sys.stderr)
    if a.all or a.unchecked or a.collection:
        if a.collection:
            ids = s.lib.collection_item_ids(a.collection)
        else:
            ids = [r[0] for r in s.lib.q("SELECT id FROM items WHERE deleted = 0" +
                                         (" AND verify_status IN ('unchecked','error')" if a.unchecked else ""))]
        t0 = time.time()

        def prog(done, total, it):
            rate = done / max(0.1, time.time() - t0)
            print(f"\r  {done}/{total}  ({rate:.1f}/s)  @{it.get('citekey', '')[:40]:<40}", end="", file=sys.stderr, flush=True)

        mode = "corrections" if a.correct else a.fix
        res = s.verifier.verify_many(ids, apply_safe=mode, workers=int(s.cfg.get("verify_concurrency", 3)), progress=prog)
        print(file=sys.stderr)
        s.flush()
        _print(res)
        return
    out = s.verify(a.citekeys, apply_safe="corrections" if a.correct else a.fix, background_over=10**9)
    s.flush()
    for r in out["results"]:
        print(f"@{r['citekey']}: {r['status']} — {r['summary']}")
        for i in r["issues"]:
            print(f"    - {i}")
        for sg in r["suggestions"]:
            tier = " safe" if sg.get("safe") else (" correction" if sg.get("correction") else "")
            print(f"    [{sg['id']}{tier}] {sg['kind']} {sg.get('field') or ''}: {sg.get('reason')}")


def cmd_fix(a) -> None:
    s = _svc()
    which = a.ids or ("corrections" if a.corrections else ("safe" if not a.keep else None))
    r = s.apply_fixes(a.citekey, which, keep=a.keep or [])
    s.flush()
    _print(r)


def cmd_export(a) -> None:
    s = _svc()
    out = s.export(a.format, a.citekeys or None, a.collection, a.out)
    if "text" in out:
        sys.stdout.write(out["text"])
    else:
        print(f"Wrote {out['count']} references to {out['path']}")


def cmd_cite(a) -> None:
    s = _svc()
    r = s.formatter.cite_strings(a.citations, a.style)
    for c in r["citations"]:
        print(c["text"])
    if r["bibliography"]:
        print()
        for e in r["bibliography"]:
            print(e["text"])
    for w in r["warnings"]:
        print(f"! {w}", file=sys.stderr)


def cmd_check_tex(a) -> None:
    s = _svc()
    rep = s.check_latex(a.tex, write_bib=a.write_bib, biblatex=a.biblatex, overwrite=a.overwrite,
                        import_missing=a.import_missing)
    s.flush()
    if a.json:
        _print(rep)
        return
    if rep.get("imported"):
        imp = rep["imported"]
        print(f"Imported {len(imp['added'])} cited entries from the document's .bib"
              + (f"; still missing: {', '.join(imp['still_missing'])}" if imp.get("still_missing") else ""))
    print(rep["summary"])
    for m in rep["missing"]:
        sug = ", ".join("@" + x["citekey"] for x in m["suggestions"][:3])
        print(f"  MISSING  {m['key']}  (used at {', '.join(m['used_at'][:2])})" + (f"  did you mean {sug}?" if sug else ""))
    for p in rep["problems"]:
        print(f"  {p['status'].upper():<9} {p['key']}: {p.get('summary') or ''}")
    for u in rep["unverified"][:30]:
        print(f"  {u['status']:<9} {u['key']}")
    for f in rep.get("fixable") or []:
        print(f"  FIXABLE   {f['key']}: {'; '.join(x for x in f['fixes'] if x)[:150]}")
    diffs = rep.get("manuscript_vs_library") or []
    if diffs:
        print("Where the document's own .bib and the library disagree ('+' = the document's .bib looks more complete):")
        for d in diffs[:60]:
            print(f"  {'+' if d['manuscript_better'] else ' '} {d['key']} {d['field']}: {d['note']}\n"
                  f"      document: {str(d['manuscript'])[:110]}\n      library:  {str(d['library'])[:110]}")
        if len(diffs) > 60:
            print(f"  ... and {len(diffs) - 60} more (use --json for all)")
    if rep.get("wrote_bib"):
        print(f"Wrote {rep['wrote_bib']}")


def cmd_render_docx(a) -> None:
    s = _svc()
    from .docx_render import render_docx
    _print(render_docx(s.formatter, a.docx, style=a.style, out_path=a.out))


def cmd_styles(a) -> None:
    s = _svc()
    if a.install:
        p = s.styles.install(a.install)
        print(f"Installed {p.stem} -> {p}")
        return
    for st in s.styles.list():
        print(f"{st['id']:<55} {st['format']:<12} {st['title']}")


def cmd_duplicates(a) -> None:
    s = _svc()
    groups = s.lib.duplicate_groups()
    for g in groups:
        print("---")
        for it in g:
            print(f"  @{it['citekey']:<45} {it['csl'].get('DOI') or '':<35} {it['csl'].get('title', '')[:60]}")
    print(f"{len(groups)} groups")


def cmd_serve(a) -> None:
    from .web import serve
    serve(port=a.port, open_browser=a.open)


def cmd_mcp(a) -> None:
    from .mcp_server import main
    main()


def cmd_config(a) -> None:
    cfg = Config()
    if a.action == "show" or not a.action:
        shown = {k: ("(set)" if k in SECRET_KEYS and v else v) for k, v in cfg.data.items()}
        _print(shown)
        for w in setup_warnings(cfg):
            print(f"! {w}", file=sys.stderr)
        return
    if not a.key:
        sys.exit("Give a setting name, e.g.: rumil config set contact_email you@example.edu")
    if a.key not in DEFAULTS:
        sys.exit(f"Unknown setting '{a.key}'. Settings: {', '.join(DEFAULTS)}")
    if a.action == "get":
        v = cfg.get(a.key)
        print("(set)" if a.key in SECRET_KEYS and v else json.dumps(v))
        return
    if a.value is None:
        sys.exit("Give a value to set")
    cur = DEFAULTS[a.key]
    val = a.value
    if isinstance(cur, bool):
        val = a.value.lower() in ("1", "true", "yes", "on")
    elif isinstance(cur, int):
        val = int(a.value)
    elif isinstance(cur, (list, dict)):
        val = json.loads(a.value)
    cfg.set(a.key, val)
    cfg.save()
    print(f"Set {a.key}. Restart the Claude app (and run 'rumil install-service' again on macOS) so running Rumil "
          "servers pick it up.")


def cmd_doctor(a) -> None:
    cfg = Config()
    print(f"Rumil {__version__}  Python {platform.python_version()}  ({sys.executable})")
    print(f"Folder: {cfg.home}  exists={cfg.home.exists()}")
    print(f"Zotero: {cfg.zotero_dir}  zotero.sqlite={'yes' if (cfg.zotero_dir / 'zotero.sqlite').exists() else 'NO'}")
    print(f"Crossref polite pool: {'yes (' + cfg.get('contact_email') + ')' if cfg.get('contact_email') else 'no (set contact_email for faster checks)'}")
    print(f"NASA ADS: {'token set' if cfg.get('ads_token') else 'no token'}")
    for w in setup_warnings(cfg):
        print(f"  ! {w}")
    from .http import Http
    from .resolvers import Resolver
    r = Resolver(Http(cfg.get("contact_email", "")), cfg.get("ads_token", ""))
    def need(res, what="record"):
        if res is None:
            raise RuntimeError(f"no {what} returned")
        return res

    def handle():
        ok = r.doi_exists("10.3390/rs15020314")
        if ok is not True:
            raise RuntimeError("doi.org did not confirm a known DOI")
        return "DOI registry reachable"

    def retraction():
        rec = need(r.crossref_work("10.1177/1758835920922055")).registry_record
        if not rec["retracted"]:
            raise RuntimeError("known retraction not detected")
        return "known retraction detected"

    checks = [
        ("Crossref (DOI 10.1130/G50341.1)", lambda: need(r.crossref_work("10.1130/G50341.1")).csl["title"]),
        ("DataCite (DOI 10.5281/zenodo.5789166)", lambda: need(r.datacite("10.5281/zenodo.5789166")).csl["title"]),
        ("doi.org handle check", handle),
        ("arXiv (2208.03533)", lambda: need(r.arxiv("2208.03533")).csl["title"]),
        ("Open Library / Google Books (ISBN 9780521853712)", lambda: need(r.isbn("9780521853712"), "book").csl["title"]),
        ("Retraction check (10.1177/1758835920922055)", retraction),
    ]
    if cfg.get("ads_token"):
        checks.append(("NASA ADS", lambda: f"{len(r.ads_query('bibcode:2022Geo....50.1182P', 1))} record"))
    for label, fn in checks:
        try:
            t0 = time.time()
            val = fn()
            print(f"  ok    {label}: {str(val)[:70]}  ({time.time() - t0:.1f}s)")
        except Exception as e:
            print(f"  FAIL  {label}: {e}")
    try:
        from .formatting import StyleRegistry
        si = StyleRegistry(cfg).get(None)
        print(f"Default style: {si.title} ({si.citation_format})")
    except Exception as e:
        print(f"Default style problem: {e}")
    try:
        from .updater import check
        u = check(cfg)
        print(f"Updates: Rumil {u['latest']} is available; run 'rumil update'" if u["update_available"]
              else f"Updates: up to date (latest release {u['latest']})")
    except Exception as e:
        print(f"Updates: couldn't check ({e})")


def cmd_update(a) -> None:
    from .updater import apply, check
    cfg = Config()
    try:
        info = check(cfg)
    except Exception as e:
        sys.exit(f"Couldn't check for updates at github.com/{cfg.get('update_repo')}: {e}")
    print(f"Installed: Rumil {info['current']}   Latest release: {info['latest']}  ({info.get('page') or ''})")
    if not info["update_available"] and not a.force:
        print("Rumil is up to date.")
        return
    if a.check:
        print("Run 'rumil update' to install it.")
        return
    if info.get("notes"):
        print("\n" + info["notes"][:1500] + "\n")
    if not a.yes:
        if not sys.stdin.isatty():
            sys.exit("Add --yes to update without a prompt.")
        if input(f"Install Rumil {info['latest']} now? Your library and settings are kept. [Y/n] ").strip().lower() \
                not in ("", "y", "yes"):
            return
    out = apply(cfg, info)
    for line in out["done"]:
        print(f"  - {line}")
    print("Quit and reopen the Claude desktop app (and start new Claude Code sessions) to use the new version there.")


def _claude_desktop_config() -> Path:
    """Where the Claude desktop app keeps its MCP server list on this system."""
    if platform.system() == "Windows":
        return Path(os.environ.get("APPDATA") or "~/AppData/Roaming").expanduser() / "Claude" / "claude_desktop_config.json"
    if platform.system() == "Darwin":
        return Path("~/Library/Application Support/Claude/claude_desktop_config.json").expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "Claude" / "claude_desktop_config.json"


CLAUDE_DESKTOP_CONFIG = _claude_desktop_config()


def cmd_install_claude(a) -> None:
    exe = shutil.which("rumil") or str(Path(sys.executable).with_name("rumil.exe" if os.name == "nt" else "rumil"))
    entry = {"command": exe, "args": ["mcp"]}
    if os.environ.get("RUMIL_HOME"):
        entry["env"] = {"RUMIL_HOME": os.environ["RUMIL_HOME"]}
    path = Path(a.config).expanduser() if a.config else CLAUDE_DESKTOP_CONFIG
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            print(f"{path} is not valid JSON; not touching it. Add this under mcpServers yourself:\n" + json.dumps({"rumil": entry}, indent=2))
            sys.exit(1)
        backup = path.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"Backed up existing config to {backup}")
    data.setdefault("mcpServers", {})["rumil"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Registered the Rumil MCP server in {path}")
    print("Quit and reopen the Claude desktop app to load it.")
    if shutil.which("claude") and not os.environ.get("RUMIL_INSTALLER"):
        print("Claude Code detected; to add it there too run:\n  claude mcp add --scope user rumil -- " + exe + " mcp")


SERVICE_LABEL = "local.rumil.library"
LAUNCH_AGENT = Path(f"~/Library/LaunchAgents/{SERVICE_LABEL}.plist").expanduser()
# Names earlier versions used; install-service and uninstall-service remove them.
LEGACY_AGENTS = [Path("~/Library/LaunchAgents/com.oldpueblolabs.rumil.plist").expanduser()]


def cmd_install_service(a) -> None:
    if platform.system() != "Darwin":
        print("The background service installer is for macOS. Run `rumil serve` instead.")
        return
    exe = shutil.which("rumil") or str(Path(sys.executable).with_name("rumil.exe" if os.name == "nt" else "rumil"))
    home = str(rumil_home())
    log = Path(home) / "rumil-web.log"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{SERVICE_LABEL}</string>
  <key>ProgramArguments</key><array><string>{exe}</string><string>serve</string></array>
  <key>EnvironmentVariables</key><dict><key>RUMIL_HOME</key><string>{home}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    for old in LEGACY_AGENTS:
        if old.exists():
            subprocess.run(["launchctl", "unload", str(old)], capture_output=True)
            old.unlink()
    subprocess.run(["launchctl", "unload", str(LAUNCH_AGENT)], capture_output=True)
    LAUNCH_AGENT.write_text(plist)
    subprocess.run(["launchctl", "load", str(LAUNCH_AGENT)], check=False)
    port = Config().get("web_port", 23120)
    print(f"Rumil's library window now runs in the background at http://127.0.0.1:{port} (starts at login).")


def cmd_uninstall_service(a) -> None:
    found = [p for p in [LAUNCH_AGENT, *LEGACY_AGENTS] if p.exists()]
    for p in found:
        subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
        p.unlink()
    print("Background service removed." if found else "No background service installed.")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="rumil", description="Rumil: a verified citation library for Claude, LaTeX, and Word.")
    p.add_argument("--version", action="version", version=f"rumil {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create ~/Rumil, settings, and copy your Zotero CSL styles")
    s.add_argument("--zotero-dir")
    s.add_argument("--email", help="contact email for Crossref's polite pool (optional)")
    s.add_argument("--ads-token", help="NASA ADS API token (optional)")
    s.add_argument("--style", help="default citation style id")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("import-zotero", help="import / re-sync your Zotero library")
    s.add_argument("--zotero-dir")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_import_zotero)

    s = sub.add_parser("import", help="import a .bib, .ris, or CSL-JSON file")
    s.add_argument("file")
    s.add_argument("--collection")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-verify", action="store_true", help="don't check new entries against the registries")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("add", help="add references by DOI, arXiv id, ISBN, PMID, ADS bibcode, or URL")
    s.add_argument("identifiers", nargs="+")
    s.add_argument("--collection")
    s.add_argument("--tag", action="append")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("search", help="search the library")
    s.add_argument("query", nargs="*")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--collection")
    s.add_argument("--status")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("show", help="show one reference")
    s.add_argument("citekey")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("verify", help="verify references against Crossref/DataCite/arXiv/ADS")
    s.add_argument("citekeys", nargs="*")
    s.add_argument("--all", action="store_true", help="the whole library")
    s.add_argument("--unchecked", action="store_true", help="only items never checked (or that errored)")
    s.add_argument("--collection")
    s.add_argument("--fix", action="store_true", help="apply safe fixes (fill empty fields, remove junk)")
    s.add_argument("--correct", action="store_true",
                   help="also apply registry corrections (title wording, author names, volume/issue/pages) to "
                        "references that are otherwise confirmed")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("fix", help="apply verification suggestions to one reference")
    s.add_argument("citekey")
    s.add_argument("ids", nargs="*", help="suggestion ids (default: the safe ones)")
    s.add_argument("--corrections", action="store_true", help="apply safe fixes and registry corrections")
    s.add_argument("--keep", action="append", metavar="FIELD",
                   help="keep the library's value for FIELD (e.g. title) and stop flagging it")
    s.set_defaults(fn=cmd_fix)

    s = sub.add_parser("export", help="export bibtex / biblatex / csljson / ris")
    s.add_argument("citekeys", nargs="*")
    s.add_argument("--format", default="bibtex")
    s.add_argument("--collection")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("cite", help="format citations, e.g. rumil cite --style icarus '[@key1; @key2]' @key3")
    s.add_argument("citations", nargs="+")
    s.add_argument("--style")
    s.set_defaults(fn=cmd_cite)

    s = sub.add_parser("check-tex", help="audit a LaTeX manuscript's citations")
    s.add_argument("tex")
    s.add_argument("--write-bib")
    s.add_argument("--biblatex", action="store_true")
    s.add_argument("--overwrite", action="store_true", help="replace an existing .bib that Rumil didn't write")
    s.add_argument("--import-missing", action="store_true",
                   help="import cited entries found only in the document's .bib, then verify them")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_check_tex)

    s = sub.add_parser("render-docx", help="format [@key] citations in a Word document (writes a new file)")
    s.add_argument("docx")
    s.add_argument("--style")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_render_docx)

    s = sub.add_parser("styles", help="list or install CSL styles")
    s.add_argument("--install")
    s.set_defaults(fn=cmd_styles)

    s = sub.add_parser("duplicates", help="list likely duplicate references")
    s.set_defaults(fn=cmd_duplicates)

    s = sub.add_parser("serve", help="run the library window (web app) on 127.0.0.1")
    s.add_argument("--port", type=int)
    s.add_argument("--open", action="store_true", help="open it in the browser")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("mcp", help="run the MCP server for Claude (stdio)")
    s.set_defaults(fn=cmd_mcp)

    s = sub.add_parser("config", help="show or change settings: rumil config set contact_email you@example.edu")
    s.add_argument("action", nargs="?", choices=["show", "get", "set"], default="show")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("doctor", help="check settings, registry connectivity, and whether an update is available")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("update", help="install the latest Rumil release from GitHub (your library and settings are kept)")
    s.add_argument("--check", action="store_true", help="only report whether an update is available")
    s.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    s.add_argument("--force", action="store_true", help="reinstall even if this version is already installed")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("install-claude", help="register the MCP server with the Claude desktop app")
    s.add_argument("--config", help="path to claude_desktop_config.json")
    s.set_defaults(fn=cmd_install_claude)

    s = sub.add_parser("install-service", help="keep the library window running in the background (macOS)")
    s.set_defaults(fn=cmd_install_service)

    s = sub.add_parser("uninstall-service", help="remove the background service")
    s.set_defaults(fn=cmd_uninstall_service)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
