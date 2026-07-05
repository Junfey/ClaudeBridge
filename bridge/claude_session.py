"""Claude session wrapper — uses interactive mode (no -p) to bill subscription pool.

Trick borrowed from `bot enakievo/ai_pty.py` (validated in prod since 2026-06-18):
spawning claude without -p and feeding the prompt via stdin classifies as
interactive use on Anthropic's side.

Sessions can be:
- new: we generate a UUID, pass --session-id, conversation file gets created
- resumed: we pass --resume <existing-uuid>, claude continues that conversation

Either way the file at ~/.claude/projects/<key>/<uuid>.jsonl is the same one
VS Code Claude can /resume.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator


def _resolve_claude_bin() -> str:
    ext_dir = Path(r"C:\Users\Admin\.vscode\extensions")
    if ext_dir.exists():
        candidates: list[tuple[tuple[int, int, int], str]] = []
        for sub in ext_dir.glob("anthropic.claude-code-*-win32-x64"):
            m = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", sub.name)
            if not m:
                continue
            ver = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            bin_path = sub / "resources" / "native-binary" / "claude.exe"
            if bin_path.exists():
                candidates.append((ver, str(bin_path)))
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]

    on_path = shutil.which("claude")
    if on_path:
        return on_path
    appdata = os.environ.get("APPDATA")
    if appdata:
        for ext in ("claude.cmd", "claude.exe"):
            p = os.path.join(appdata, "npm", ext)
            if os.path.exists(p):
                return p
    raise RuntimeError("claude binary not found")


@dataclass
class ChatTurn:
    role: str
    text: str


@dataclass
class ClaudeSession:
    id: str  # bridge-local id (same as claude session UUID for new sessions)
    cwd: str
    claude_session_id: str  # UUID — passed to --session-id or --resume
    title: str = ""
    is_resumed: bool = False  # True if this points to an existing on-disk session
    busy: bool = False
    timeout_seconds: float = 300.0

    async def send(self, message: str) -> AsyncIterator[dict]:
        if self.busy:
            raise RuntimeError("Session is busy")
        self.busy = True
        if not self.title:
            self.title = message[:60]

        bin_path = _resolve_claude_bin()
        if self.is_resumed:
            cmd = [bin_path, "--resume", self.claude_session_id]
        else:
            cmd = [bin_path, "--session-id", self.claude_session_id]

        try:
            yield {"type": "started"}
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(input=message.encode("utf-8")),
                    timeout=self.timeout_seconds,
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                yield {"type": "error", "text": f"timeout after {int(self.timeout_seconds)}s"}
                return

            if proc.returncode != 0:
                err = stderr_b.decode("utf-8", errors="replace").strip()
                yield {"type": "error", "text": f"claude exited {proc.returncode}: {err[:500]}"}
                return

            text = stdout_b.decode("utf-8", errors="replace").strip()
            if not text:
                yield {"type": "error", "text": "empty response from claude"}
                return

            # After first send, subsequent sends should resume
            self.is_resumed = True

            yield {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            }
        finally:
            self.busy = False


def new_session(cwd: str) -> ClaudeSession:
    sid = str(uuid.uuid4())
    return ClaudeSession(id=sid, cwd=cwd, claude_session_id=sid, is_resumed=False)


def resume_session(cwd: str, claude_session_id: str, title: str = "") -> ClaudeSession:
    return ClaudeSession(
        id=claude_session_id,
        cwd=cwd,
        claude_session_id=claude_session_id,
        title=title,
        is_resumed=True,
    )
