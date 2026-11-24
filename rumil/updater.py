"""Update Rumil from its GitHub releases (`rumil update`).

A release carries a zip of the source (rumil-vX.Y.Z.zip). Updating installs that into Rumil's own Python
environment, refreshes the browser button and the Claude skill, and restarts the library window. The library,
settings, and styles in ~/Rumil are never touched.
"""

from __future__ import annotations

import io
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from . import __version__
from .config import Config
from .http import Http, HttpError

CHECK_CACHE = "update-check.json"
CHECK_EVERY = 24 * 3600  # seconds; GitHub allows 60 unauthenticated API calls an hour


def version_tuple(v: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", v or "")[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def latest_release(http: Http, repo: str) -> dict:
    data = http.get_json(f"https://api.github.com/repos/{repo}/releases/latest", accept="application/vnd.github+json",
                         retries=0, timeout=10)
    if not data or not data.get("tag_name"):
        raise HttpError(f"No published releases found for {repo}")
    asset = next((a for a in data.get("assets") or []
                  if re.fullmatch(r"rumil-v?\d+(?:\.\d+)*\.zip", a.get("name") or "")), None)
    return {"version": data["tag_name"].lstrip("vV"), "tag": data["tag_name"],
            "url": asset["browser_download_url"] if asset else data.get("zipball_url"),
            "page": data.get("html_url"), "notes": (data.get("body") or "").strip()}


def check(cfg: Config, http: Http | None = None, *, use_cache: bool = False) -> dict:
    """{'current', 'latest', 'update_available', 'page', 'notes'}; with use_cache, asks GitHub at most once a day."""
    cache = cfg.home / CHECK_CACHE
    if use_cache:
        try:
            c = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() - c.get("checked_at", 0) < CHECK_EVERY and c.get("current") == __version__:
                return c
        except (OSError, ValueError):
            pass
    rel = latest_release(http or Http(cfg.get("contact_email", "")), cfg.get("update_repo") or "michael-s-phillips/rumil")
    out = {"current": __version__, "latest": rel["version"], "page": rel["page"], "url": rel["url"],
           "notes": rel["notes"][:2000], "update_available": version_tuple(rel["version"]) > version_tuple(__version__),
           "checked_at": time.time()}
    try:
        cache.write_text(json.dumps(out), encoding="utf-8")
    except OSError:
        pass
    return out


def _safe_extract(data: bytes, dest: Path) -> Path:
    """Unzip a release into dest (refusing paths that would land outside it); return the source folder."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = dest.resolve()
        for m in z.infolist():
            target = (dest / m.filename).resolve()
            if root != target and root not in target.parents:
                raise ValueError(f"Unsafe path in release archive: {m.filename}")
        z.extractall(dest)
    for p in [dest, *sorted(dest.rglob("pyproject.toml"), key=lambda q: len(q.parts))]:
        d = p if p.is_dir() else p.parent
        if (d / "pyproject.toml").exists() and (d / "rumil" / "__init__.py").exists():
            return d
    raise ValueError("The release archive doesn't contain the Rumil source")


def apply(cfg: Config, info: dict, http: Http | None = None, log=print) -> dict:
    """Download and install the release described by `info` (from check())."""
    http = http or Http(cfg.get("contact_email", ""))
    log(f"Downloading Rumil {info['latest']} ...")
    resp = http.request(info["url"], timeout=120)
    if not resp.ok:
        raise HttpError(f"Download failed (HTTP {resp.status})", resp.status, info["url"])
    with tempfile.TemporaryDirectory(prefix="rumil-update-") as tmp:
        src = _safe_extract(resp.body, Path(tmp))
        log("Installing into Rumil's Python environment ...")
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", str(src)], check=True)
        done = [f"Rumil {info['latest']} installed"]
        if (src / "chrome-extension").is_dir():
            ext = cfg.home / "chrome-extension"
            shutil.rmtree(ext, ignore_errors=True)
            shutil.copytree(src / "chrome-extension", ext)
            done.append("browser button files refreshed (reload it in chrome://extensions)")
        skill = src / "skill" / "rumil-citations" / "SKILL.md"
        if skill.exists():
            with zipfile.ZipFile(cfg.home / "rumil-citations-skill.zip", "w", zipfile.ZIP_DEFLATED) as z:
                z.write(skill, "rumil-citations/SKILL.md")
            cc = Path.home() / ".claude" / "skills" / "rumil-citations" / "SKILL.md"
            if cc.exists():
                shutil.copy2(skill, cc)
                done.append("Claude Code skill updated")
            done.append(f"skill zip for Claude's settings refreshed ({cfg.home / 'rumil-citations-skill.zip'})")
    if platform.system() == "Darwin":
        from .cli import LAUNCH_AGENT, LEGACY_AGENTS
        if LAUNCH_AGENT.exists() or any(p.exists() for p in LEGACY_AGENTS):
            # A fresh process, so the newly installed code writes and loads the service definition.
            subprocess.run([sys.executable, "-m", "rumil.cli", "install-service"], check=False)
            done.append("library window restarted")
    try:
        (cfg.home / CHECK_CACHE).unlink()
    except OSError:
        pass
    return {"installed": info["latest"], "done": done}
