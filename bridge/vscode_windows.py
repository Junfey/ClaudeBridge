"""Detect open VS Code windows on Windows and extract workspace info.

Windows-only (win32 API). On other platforms these imports are absent; the app
uses the cross-platform CDP path instead, so we degrade to an empty list."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

try:
    import psutil
    import win32gui
    import win32process
    _WIN32 = True
except Exception:  # non-Windows (e.g. macOS build) — win32 unavailable
    _WIN32 = False

TITLE_SUFFIX = " - Visual Studio Code"


@dataclass
class VSCodeWindow:
    hwnd: int
    pid: int
    title: str
    workspace_name: str | None
    workspace_path: str | None


def _enum_top_level_windows() -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []

    def cb(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if title:
            out.append((hwnd, title))
        return True

    win32gui.EnumWindows(cb, None)
    return out


def _parse_workspace_name(title: str) -> str | None:
    if not title.endswith(TITLE_SUFFIX):
        return None
    body = title[: -len(TITLE_SUFFIX)].rstrip()
    parts = [p.strip() for p in body.split(" - ") if p.strip()]
    if not parts:
        return None
    name = parts[-1]
    return re.sub(r"^[●•\*\s]+", "", name) or None


def _known_workspace_paths() -> dict[str, str]:
    """Read VS Code recents to map workspace name -> full path."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return {}
    candidates = [
        Path(appdata) / "Code" / "User" / "globalStorage" / "storage.json",
        Path(appdata) / "Code" / "storage.json",
    ]
    mapping: dict[str, str] = {}
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        for url in _extract_folder_uris(data):
            local = _file_uri_to_path(url)
            if local and Path(local).exists():
                mapping.setdefault(Path(local).name, local)
    return mapping


def _extract_folder_uris(obj: object) -> list[str]:
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "folderUri" and isinstance(v, str):
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str) and node.startswith("file:///"):
            found.append(node)

    walk(obj)
    return found


def _file_uri_to_path(uri: str) -> str | None:
    if not uri.startswith("file:///"):
        return None
    from urllib.parse import unquote

    path = unquote(uri[len("file:///") :])
    return path.replace("/", "\\")


def list_vscode_windows() -> list[VSCodeWindow]:
    if not _WIN32:
        return []
    name_to_path = _known_workspace_paths()
    pid_to_name = {p.pid: p.name() for p in psutil.process_iter(["name"])}
    result: list[VSCodeWindow] = []
    for hwnd, title in _enum_top_level_windows():
        if not title.endswith(TITLE_SUFFIX):
            continue
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid_to_name.get(pid) != "Code.exe":
            continue
        name = _parse_workspace_name(title)
        path = name_to_path.get(name) if name else None
        result.append(
            VSCodeWindow(
                hwnd=hwnd, pid=pid, title=title,
                workspace_name=name, workspace_path=path,
            )
        )
    return result


if __name__ == "__main__":
    for w in list_vscode_windows():
        print(f"[{w.pid}] {w.workspace_name!r} path={w.workspace_path} title={w.title!r}")
