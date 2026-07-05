"""Read Claude Code's on-disk session storage.

Sessions are stored as JSONL at:
  ~/.claude/projects/<project-key>/<session-uuid>.jsonl

The project key is the workspace path with all non-alphanumeric chars
replaced by '-' (e.g. C:\\Creation\\FriendlyTradeBot → C--Creation-FriendlyTradeBot).

Each line is one event in the conversation. Types include user, assistant,
queue-operation, summary, and various tool-related entries; we care about
user and assistant entries for display.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def project_path_to_key(path: str) -> str:
    # Claude replaces EVERY non-alphanumeric char (incl. '.' and '_') with '-'.
    # e.g. C:\Creation\Gemalli\gemalli.com_prod -> C--Creation-Gemalli-gemalli-com-prod
    return "".join(ch if ch.isalnum() else "-" for ch in path)


@dataclass
class SessionSummary:
    id: str
    project_key: str
    mtime: float
    size: int
    title: str
    turns: int


@dataclass
class SessionTurn:
    role: str
    text: str
    timestamp: str


def list_sessions(project_path: str, limit: int = 30) -> list[SessionSummary]:
    key = project_path_to_key(project_path)
    proj_dir = CLAUDE_PROJECTS / key
    if not proj_dir.exists():
        return []
    files = sorted(
        proj_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[: limit * 2]  # over-fetch in case some are empty
    out: list[SessionSummary] = []
    for f in files:
        try:
            s = _summarize(f, key)
        except Exception:
            continue
        if s is not None:
            out.append(s)
            if len(out) >= limit:
                break
    return out


_CHUNK = 96 * 1024  # read only head + tail — keeps huge (100MB+) files fast


def _parse_lines(chunk: str):
    for line in chunk.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _summarize(path: Path, key: str) -> SessionSummary | None:
    try:
        st = path.stat()
    except OSError:
        return None
    size = st.st_size
    with path.open("rb") as f:
        head = f.read(_CHUNK)
        if size > _CHUNK * 2:
            f.seek(size - _CHUNK)
            tail = f.read(_CHUNK)
        else:
            tail = b""
    head_s = head.decode("utf-8", errors="replace")
    tail_s = tail.decode("utf-8", errors="replace")

    # Title: prefer the latest ai-title (usually near the end), else first user msg.
    title = ""
    for evt in _parse_lines(tail_s):
        if evt.get("type") == "ai-title" and evt.get("aiTitle"):
            title = evt["aiTitle"].strip()[:80]
    if not title:
        for evt in _parse_lines(head_s):
            if evt.get("type") == "ai-title" and evt.get("aiTitle"):
                title = evt["aiTitle"].strip()[:80]
                break
    if not title:
        for evt in _parse_lines(head_s):
            if evt.get("type") == "user":
                text = _extract_text(evt.get("message", {}))
                if text and not text.startswith("Caveat:"):
                    title = text.strip()[:80]
                    break

    # Turn count: estimate from assistant markers in the sampled chunks scaled
    # by file size (exact counting would require reading the whole file).
    sampled = head_s + tail_s
    marker = sampled.count('"type":"assistant"')
    if size > _CHUNK * 2 and marker > 0:
        turns = max(marker, int(marker * size / (len(head) + len(tail) + 1)))
    else:
        turns = marker

    if turns == 0 and not title:
        return None
    return SessionSummary(
        id=path.stem,
        project_key=key,
        mtime=st.st_mtime,
        size=size,
        title=title or "(no title)",
        turns=max(turns, 1),
    )


def _extract_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    t = block.get("text")
                    if isinstance(t, str):
                        parts.append(t)
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _ai_title(path: Path) -> str:
    """Claude stores the tab's generated title as an {"type":"ai-title",
    "aiTitle": ...} event, updated as the chat grows and written near the end.
    Read only the file tail (+ a head fallback) so huge files stay fast."""
    title = ""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > 128 * 1024:
                f.seek(size - 128 * 1024)
                chunks = [f.read()]
            else:
                chunks = [f.read()]
    except OSError:
        return ""
    for chunk in chunks:
        for line in chunk.decode("utf-8", errors="replace").split("\n"):
            if '"ai-title"' not in line:
                continue
            try:
                evt = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "ai-title" and evt.get("aiTitle"):
                title = evt["aiTitle"]
    return title.strip()


def build_title_index(max_scan: int = 150) -> list[tuple[str, Path]]:
    """Return [(ai_title_lower, path), ...] for recent sessions, newest first.
    Built once per tab-list refresh so we don't re-scan files per tab."""
    if not CLAUDE_PROJECTS.exists():
        return []
    files = sorted(
        CLAUDE_PROJECTS.glob("*/*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:max_scan]
    out: list[tuple[str, Path]] = []
    for f in files:
        at = _ai_title(f).lower().rstrip("…").strip()
        if at:
            out.append((at, f))
    return out


def match_title(title: str, index: list[tuple[str, Path]]) -> Path | None:
    """Match a tab title against a prebuilt title index."""
    norm = (title or "").strip().rstrip("…").lower()
    if not norm:
        return None
    for at, f in index:
        if at == norm or at.startswith(norm) or norm.startswith(at[:40]):
            return f
    return None


def find_session_file_by_title(title: str, max_scan: int = 120) -> Path | None:
    """Map a live CDP tab (by its generated title) to its JSONL file by matching
    the tab title against each session's ai-title event. Newest files first."""
    title = (title or "").strip().rstrip("…")
    if not title or not CLAUDE_PROJECTS.exists():
        return None
    files = sorted(
        CLAUDE_PROJECTS.glob("*/*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:max_scan]
    norm = title.lower()
    for f in files:
        at = _ai_title(f).lower().rstrip("…")
        if not at:
            continue
        if at == norm or at.startswith(norm) or norm.startswith(at[:40]):
            return f
    return None


def load_history(session_id: str, project_path: str, limit_turns: int = 100) -> list[SessionTurn]:
    key = project_path_to_key(project_path)
    path = CLAUDE_PROJECTS / key / f"{session_id}.jsonl"
    if not path.exists():
        return []
    turns: list[SessionTurn] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = evt.get("type")
            if t not in ("user", "assistant"):
                continue
            text = _extract_text(evt.get("message", {}))
            if not text:
                continue
            turns.append(
                SessionTurn(role=t, text=text, timestamp=evt.get("timestamp", ""))
            )
    if len(turns) > limit_turns:
        turns = turns[-limit_turns:]
    return turns
