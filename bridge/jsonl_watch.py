"""Watch Claude's JSONL session files and stream new conversation events.

Reading strategy (robust, class-free):
  - History comes from the session .jsonl (clean, canonical).
  - After we inject a message via CDP, we don't know which session file the
    tab writes to. So we snapshot mtimes across all project dirs, then poll
    for the file whose mtime advanced past the snapshot — that's the active
    session. We lock onto it and tail new lines as Claude writes them.

Event shape emitted to callers:
  {"type": "user"|"assistant"|"tool_use"|"tool_result", "text": str, ...}
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def _all_session_files() -> list[Path]:
    if not CLAUDE_PROJECTS.exists():
        return []
    return list(CLAUDE_PROJECTS.glob("*/*.jsonl"))


def snapshot_sizes() -> dict[str, int]:
    """Map every session file path -> current byte size (pre-inject baseline)."""
    out: dict[str, int] = {}
    for f in _all_session_files():
        try:
            out[str(f)] = f.stat().st_size
        except OSError:
            continue
    return out


async def wait_for_grown_file(
    baseline: dict[str, int], timeout: float = 25.0
) -> tuple[Path, int] | None:
    """Poll until some session file grows past its baseline size.

    Returns (path, baseline_size) so the caller tails strictly from the byte
    where Claude's new turn begins — no re-reading already-shown content.
    New files (absent from baseline) are treated as baseline 0.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for f in _all_session_files():
            key = str(f)
            base = baseline.get(key, 0)
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if size > base:
                return f, base
        await asyncio.sleep(0.35)
    return None


def newest_file_since(since_ts: float, project_key: str | None = None) -> Path | None:
    """Return the .jsonl modified most recently after `since_ts`."""
    best: Path | None = None
    best_m = since_ts
    root = CLAUDE_PROJECTS / project_key if project_key else CLAUDE_PROJECTS
    pattern = "*.jsonl" if project_key else "*/*.jsonl"
    if not root.exists():
        return None
    for f in root.glob(pattern):
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        if m > best_m:
            best_m = m
            best = f
    return best


def _extract_event(evt: dict) -> dict | None:
    t = evt.get("type")
    if t == "user":
        # A user message may carry a tool_result (output of a tool Claude ran).
        msg = evt.get("message", {})
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    out = _tool_result_text(block.get("content"))
                    if out:
                        return {"type": "tool_result", "text": out[:2000]}
        text = _content_text(msg)
        if text:
            return {"type": "user", "text": text}
        return None
    if t == "assistant":
        msg = evt.get("message", {})
        content = msg.get("content", [])
        texts = []
        tools = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                elif block.get("type") == "tool_use":
                    tools.append({
                        "name": block.get("name", "tool"),
                        "input": block.get("input", {}),
                    })
        out = {"type": "assistant", "text": "".join(texts).strip()}
        if tools:
            out["tools"] = tools
        # stop_reason tells us whether the turn is truly over ("end_turn") or
        # Claude is about to call a tool ("tool_use"). Used to detect turn end
        # reliably instead of guessing from file-quiet timing.
        stop = msg.get("stop_reason")
        if stop:
            out["stop_reason"] = stop
        if out["text"] or tools:
            return out
        return None
    return None


def _tool_result_text(content) -> str:
    """Extract plain text from a tool_result content field (str or list of blocks)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "".join(parts).strip()
    return ""


def _content_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "".join(parts)
    return ""


async def wait_for_active_session(
    since_ts: float, timeout: float = 20.0, project_key: str | None = None
) -> Path | None:
    """Poll until a session file shows activity after since_ts."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        f = newest_file_since(since_ts, project_key)
        if f is not None:
            return f
        await asyncio.sleep(0.4)
    return None


async def tail_session(path: Path, from_byte: int = 0, idle_timeout: float = 90.0):
    """Async generator: yield parsed events as they're appended to the file.

    Stops after `idle_timeout` seconds with no new bytes (assumes turn done).
    """
    pos = from_byte
    last_data = time.monotonic()
    buffer = ""
    while True:
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size > pos:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            last_data = time.monotonic()
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                parsed = _extract_event(evt)
                if parsed:
                    yield parsed
        elif time.monotonic() - last_data > idle_timeout:
            return
        await asyncio.sleep(0.3)


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def context_usage(path: Path, limit: int = 1_000_000) -> dict | None:
    """Current context window usage, from the last request's token counts in
    the JSONL (input + cache_read + cache_creation = prompt size that turn)."""
    last = None
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                u = (evt.get("message") or {}).get("usage")
                if isinstance(u, dict) and u.get("input_tokens") is not None:
                    last = u
    except OSError:
        return None
    if not last:
        return None
    tokens = (
        int(last.get("input_tokens", 0))
        + int(last.get("cache_read_input_tokens", 0))
        + int(last.get("cache_creation_input_tokens", 0))
    )
    return {"tokens": tokens, "limit": limit, "pct": round(tokens * 100 / limit)}


async def wait_until_settled(path: Path, quiet_seconds: float = 4.0, max_wait: float = 240.0) -> None:
    """Block until the file has not grown for `quiet_seconds` (turn finished),
    or `max_wait` elapses. Robust way to know Claude's turn is done."""
    start = time.monotonic()
    last_size = file_size(path)
    last_change = time.monotonic()
    while time.monotonic() - start < max_wait:
        await asyncio.sleep(0.5)
        size = file_size(path)
        if size != last_size:
            last_size = size
            last_change = time.monotonic()
        elif time.monotonic() - last_change >= quiet_seconds:
            return


def read_events_from(path: Path, from_byte: int) -> list[dict]:
    """Read and parse all conversation events from a byte offset to EOF."""
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(from_byte)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                parsed = _extract_event(evt)
                if parsed:
                    out.append(parsed)
    except OSError:
        pass
    return out
