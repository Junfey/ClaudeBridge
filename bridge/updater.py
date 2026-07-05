"""Self-update from GitHub Releases.

On start (and once a day while running) the app asks the GitHub API for the
latest release. If it's newer than VERSION, it downloads the asset for this OS
and swaps the running binary via a tiny helper script, then relaunches.

No token needed — the repo/releases are public.
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

from .version import GITHUB_REPO, VERSION


def _ver_tuple(v: str) -> tuple:
    v = (v or "").lstrip("vV").strip()
    parts = []
    for p in v.split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts) or (0,)


def is_newer(remote: str, local: str = VERSION) -> bool:
    return _ver_tuple(remote) > _ver_tuple(local)


def _asset_keyword() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def check_latest() -> dict | None:
    """Return {'version','url','notes','asset'} for the latest release, or None."""
    if "__OWNER__" in GITHUB_REPO:
        return None  # repo not configured yet
    api = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    ctx = ssl.create_default_context()
    req = urllib.request.Request(api, headers={
        "User-Agent": "ClaudeBridge-updater",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=12, context=ctx) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    tag = data.get("tag_name") or ""
    if not is_newer(tag):
        return None
    kw = _asset_keyword()
    asset = None
    for a in data.get("assets", []):
        name = (a.get("name") or "").lower()
        if kw in name and a.get("browser_download_url"):
            asset = a["browser_download_url"]
            break
    return {
        "version": tag.lstrip("vV"),
        "url": data.get("html_url", ""),
        "notes": (data.get("body") or "")[:400],
        "asset": asset,
    }


def _download(url: str, dest: Path) -> bool:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "ClaudeBridge-updater"})
    try:
        with urllib.request.urlopen(req, timeout=120, context=ctx) as r, dest.open("wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        return dest.stat().st_size > 0
    except Exception:
        try:
            dest.unlink()
        except OSError:
            pass
        return False


def apply_update(asset_url: str) -> bool:
    """Download the new binary next to the current one and swap it in via a
    helper that runs after we exit, then relaunch. Returns True if staged."""
    if not getattr(sys, "frozen", False) or not asset_url:
        return False
    cur = Path(sys.executable).resolve()
    new = cur.with_name(cur.stem + ".update" + cur.suffix)
    if not _download(asset_url, new):
        return False

    if sys.platform.startswith("win"):
        # A running .exe can't overwrite itself; a batch swaps it after we exit.
        bat = cur.with_name("_update.bat")
        bat.write_text(
            "@echo off\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            f'move /y "{new.name}" "{cur.name}" >nul\r\n'
            f'start "" "{cur}"\r\n'
            'del "%~f0"\r\n',
            encoding="utf-8",
        )
        subprocess.Popen(["cmd", "/c", str(bat)], cwd=str(cur.parent),
                         creationflags=0x08000000)  # CREATE_NO_WINDOW
    else:
        # macOS/Linux can overwrite a running binary's file directly.
        sh = cur.with_name("_update.sh")
        sh.write_text(
            "#!/bin/sh\n"
            "sleep 2\n"
            f'mv -f "{new}" "{cur}"\n'
            f'chmod +x "{cur}"\n'
            f'"{cur}" &\n',
            encoding="utf-8",
        )
        os.chmod(sh, 0o755)
        subprocess.Popen(["/bin/sh", str(sh)], cwd=str(cur.parent))
    return True
