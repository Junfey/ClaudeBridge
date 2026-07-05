"""Simple shared-token auth so a leaked tunnel URL can't hand strangers a
shell on the machine. One token per install, stored next to the code.

The token gates /api/* and /ws/*. The HTML shell is public (it just prompts
for the token). The owner reads the token from the launcher output / QR.
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

# Persist the token next to the exe when frozen (so it survives restarts),
# else next to the source. Never inside the PyInstaller temp dir.
if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).resolve().parent
else:
    _BASE = Path(__file__).resolve().parent.parent
_TOKEN_FILE = _BASE / ".bridge-token"


def get_or_create_token() -> str:
    if _TOKEN_FILE.exists():
        tok = _TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(24)
    _TOKEN_FILE.write_text(tok, encoding="utf-8")
    return tok


TOKEN = get_or_create_token()


def check(provided: str | None) -> bool:
    return bool(provided) and secrets.compare_digest(provided, TOKEN)
