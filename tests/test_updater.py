"""`rumil update`: finding the latest GitHub release and installing it without touching the library."""

import io
import json
import zipfile

import pytest

from rumil import __version__, updater
from rumil.http import Http, Response

REPO = "michael-s-phillips/rumil"


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


RELEASE_ZIP = _zip({
    "rumil/pyproject.toml": "[project]\nname='rumil'\nversion='9.9.9'\n",
    "rumil/rumil/__init__.py": "__version__ = '9.9.9'\n",
    "rumil/chrome-extension/manifest.json": "{}",
    "rumil/skill/rumil-citations/SKILL.md": "---\nname: rumil-citations\n---\nnew skill\n",
})


class GitHub:
    def __init__(self, tag="v9.9.9", status=200):
        self.tag, self.status, self.calls = tag, status, []

    def __call__(self, method, url, headers, timeout):
        self.calls.append(url)
        if url.startswith(f"https://api.github.com/repos/{REPO}/releases/latest"):
            body = {"tag_name": self.tag, "html_url": f"https://github.com/{REPO}/releases/tag/{self.tag}",
                    "body": "Fixes and improvements", "zipball_url": "https://api.github.com/zipball",
                    "assets": [{"name": f"rumil-{self.tag}.zip",
                                "browser_download_url": f"https://github.com/{REPO}/releases/download/{self.tag}/rumil-{self.tag}.zip"},
                               {"name": "rumil-9.9.9-py3-none-any.whl", "browser_download_url": "https://x/whl"}]}
            return Response(self.status, url, {"content-type": "application/json"}, json.dumps(body).encode())
        if "/releases/download/" in url:
            return Response(200, url, {"content-type": "application/zip"}, RELEASE_ZIP)
        return Response(404, url, {}, b"")


def test_version_order():
    assert updater.version_tuple("0.10.0") > updater.version_tuple("0.9.9")
    assert updater.version_tuple("v1.2") == (1, 2, 0)


def test_check_finds_the_release_zip_and_caches(cfg):
    gh = GitHub()
    info = updater.check(cfg, Http(transport=gh))
    assert info["update_available"] and info["latest"] == "9.9.9" and info["current"] == __version__
    assert info["url"].endswith("/rumil-v9.9.9.zip")
    n = len(gh.calls)
    assert updater.check(cfg, Http(transport=gh), use_cache=True)["latest"] == "9.9.9"
    assert len(gh.calls) == n  # answered from the day's cache
    assert not updater.check(cfg, Http(transport=GitHub(tag="v" + __version__)))["update_available"]


def test_apply_installs_and_refreshes_without_touching_the_library(cfg, monkeypatch):
    (cfg.home / "library.sqlite").write_bytes(b"precious")
    (cfg.home / "chrome-extension").mkdir(parents=True, exist_ok=True)
    (cfg.home / "chrome-extension" / "old.js").write_text("old")
    runs = []
    monkeypatch.setattr(updater.subprocess, "run", lambda args, **kw: runs.append(args))
    monkeypatch.setattr(updater.platform, "system", lambda: "Linux")
    gh = GitHub()
    info = updater.check(cfg, Http(transport=gh))
    out = updater.apply(cfg, info, Http(transport=gh), log=lambda *a: None)
    assert out["installed"] == "9.9.9"
    pip = runs[0]
    assert pip[1:5] == ["-m", "pip", "install", "--quiet"] and pip[-1].endswith("/rumil")
    assert (cfg.home / "chrome-extension" / "manifest.json").exists()
    assert not (cfg.home / "chrome-extension" / "old.js").exists()
    with zipfile.ZipFile(cfg.home / "rumil-citations-skill.zip") as z:
        assert z.namelist() == ["rumil-citations/SKILL.md"] and b"new skill" in z.read("rumil-citations/SKILL.md")
    assert (cfg.home / "library.sqlite").read_bytes() == b"precious"


def test_release_archive_with_unsafe_paths_is_refused(tmp_path):
    bad = _zip({"../evil.py": "x", "rumil/pyproject.toml": ""})
    with pytest.raises(ValueError):
        updater._safe_extract(bad, tmp_path)


def test_no_releases_yet(cfg):
    with pytest.raises(Exception):
        updater.check(cfg, Http(transport=GitHub(status=404)))
