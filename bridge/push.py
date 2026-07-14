"""Web Push: VAPID keys, subscription store, and a background watcher that
notifies the phone when Claude finishes a turn in a tab you're NOT watching.

Works even when the PWA is closed — the browser's push service delivers it.
"""
from __future__ import annotations

import asyncio
import base64
import json
import socket
import sys
import threading
import time
from pathlib import Path

# This network hands out IPv6 for Google hosts, and Python's requests (no
# happy-eyeballs) blocks ~30s on the IPv6 connect before falling back to IPv4.
# Prefer IPv4 so pushes to FCM go out instantly. IPv6 stays as a fallback.
_orig_getaddrinfo = socket.getaddrinfo
def _getaddrinfo_v4_first(host, *args, **kwargs):
    res = _orig_getaddrinfo(host, *args, **kwargs)
    res.sort(key=lambda r: 0 if r[0] == socket.AF_INET else 1)
    return res
socket.getaddrinfo = _getaddrinfo_v4_first

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from . import claude_storage

# Persist next to the exe (frozen) or the source, alongside the auth token.
if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).resolve().parent
else:
    _BASE = Path(__file__).resolve().parent.parent
_VAPID_FILE = _BASE / ".bridge-vapid.json"
_SUBS_FILE = _BASE / ".bridge-push.json"

_lock = threading.Lock()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _load_or_create_vapid() -> dict:
    if _VAPID_FILE.exists():
        try:
            return json.loads(_VAPID_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    key = ec.generate_private_key(ec.SECP256R1())
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    d = {"private_pem": priv_pem, "public_key": _b64(pub_raw)}
    try:
        _VAPID_FILE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass
    return d


_VAPID = _load_or_create_vapid()
PUBLIC_KEY = _VAPID["public_key"]
# VAPID subject. MUST be a `mailto:` link with a REAL domain:
#  - py_vapid REJECTS a non-mailto sub outright ("Missing 'sub' ... as a mailto:
#    link") — an https URL here silently broke ALL push (v1.0.3 regression).
#  - Apple (Safari/iOS) rejects an invalid domain like `@localhost`.
# github's noreply domain satisfies both.
_CLAIMS = {"sub": "mailto:claudebridge@users.noreply.github.com"}

# pywebpush needs a Vapid object (a PEM *string* is misread as a raw key and
# fails to deserialize). Build it once from the stored PEM.
try:
    from py_vapid import Vapid01
    _VAPID_OBJ = Vapid01.from_pem(_VAPID["private_pem"].encode())
except Exception:
    _VAPID_OBJ = None

# Session files currently mirrored by an open WS — we skip pushes for these
# (you're already looking at that chat).
ACTIVE: set[str] = set()

# Anti-spam: a session that finished a turn and was pushed about, but hasn't been
# READ yet. We push a chat AT MOST ONCE per unread episode — otherwise an
# autonomous loop (/loop, "process queue until empty") that ends a turn every few
# seconds fires a push each time and buries the phone. Cleared by mark_read() when
# you open that chat, so the NEXT completion after you've seen it notifies again.
_NOTIFIED: set[str] = set()

# Session files that are currently OPEN as VS Code Claude tabs (updated by the tab
# builder in main.py). We push ONLY for these. A background `claude` CLI / cron run
# — e.g. a task-queue processor that re-invokes claude in ~/Downloads every few
# seconds — writes session files too, each a NEW uuid with the SAME title, so
# per-file dedup can't catch it; and the user isn't driving those through the
# bridge, so they must never push. Empty ⇒ gate off (falls back to the title guard).
TAB_FILES: set[str] = set()

# Backstop for the empty-TAB_FILES case (CDP briefly down): don't push two chats
# with the SAME title within this window — squashes a runaway loop that spawns many
# new session files all titled the same.
_TITLE_AT: dict[str, float] = {}
_TITLE_COOLDOWN = 300.0  # seconds


def mark_read(key: str) -> None:
    """Called when you open a chat — you've now seen it, so allow the next
    completion to push again."""
    _NOTIFIED.discard(key)


def _load_subs() -> list[dict]:
    if _SUBS_FILE.exists():
        try:
            return json.loads(_SUBS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_subs(subs: list[dict]) -> None:
    try:
        _SUBS_FILE.write_text(json.dumps(subs), encoding="utf-8")
    except Exception:
        pass


def add_subscription(sub: dict) -> None:
    with _lock:
        subs = _load_subs()
        ep = sub.get("endpoint")
        if ep and not any(s.get("endpoint") == ep for s in subs):
            subs.append(sub)
            _save_subs(subs)


def _send_blocking(payload: dict) -> int:
    from pywebpush import webpush, WebPushException

    with _lock:
        subs = _load_subs()
    if not subs:
        return 0
    ok = 0
    dead = []
    for s in subs:
        try:
            webpush(
                subscription_info=s,
                data=json.dumps(payload),
                vapid_private_key=_VAPID_OBJ,
                vapid_claims=dict(_CLAIMS),
                timeout=10,
            )
            ok += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):  # subscription gone — drop it
                dead.append(s.get("endpoint"))
        except Exception:
            pass
    if dead:
        with _lock:
            subs = [s for s in _load_subs() if s.get("endpoint") not in dead]
            _save_subs(subs)
    return ok


async def send(payload: dict) -> int:
    """Send a push to all subscriptions (webpush is blocking → run in a thread)."""
    return await asyncio.get_event_loop().run_in_executor(None, _send_blocking, payload)


import re as _re


def snippet(text: str, limit: int = 160) -> str:
    """Turn a raw assistant/question message into a short plain-text preview for
    a push body: drop code blocks, strip markdown marks, collapse whitespace."""
    t = _re.sub(r"```.*?```", " […] ", text or "", flags=_re.S)
    t = _re.sub(r"`([^`]*)`", r"\1", t)
    t = _re.sub(r"^\s*[#>\-\*]+\s*", "", t, flags=_re.M)
    t = _re.sub(r"[*_#`]", "", t)
    t = _re.sub(r"\s+", " ", t).strip()
    return (t[: limit - 1] + "…") if len(t) > limit else t


async def watcher() -> None:
    """Watch recent session files; push once when Claude completes a turn in a
    tab that isn't currently being mirrored. Baselines existing files silently.
    The push body carries a preview of what Claude actually said."""
    from . import jsonl_watch

    seen: dict[str, int] = {}
    first = True
    while True:
        try:
            root = claude_storage.CLAUDE_PROJECTS
            files = []
            if root.exists():
                files = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]
            for f in files:
                try:
                    size = f.stat().st_size
                except OSError:
                    continue
                key = str(f)
                prev = seen.get(key)
                seen[key] = size
                if prev is None or size <= prev:
                    continue
                if key in ACTIVE:  # you're watching this chat — no push
                    continue
                # Push ONLY for real VS Code Claude tabs — never for a background
                # CLI/cron run (task-queue processor, headless `claude -p`, …). This
                # is what stopped the ~250/hr "process task queue" flood from
                # ~/Downloads. Gate off only if we have no tab list yet.
                if TAB_FILES and key not in TAB_FILES:
                    continue
                if key in _NOTIFIED:  # already pushed & not read yet — don't spam
                    continue          # (an idle loop keeps ending turns forever)
                try:
                    events = jsonl_watch.read_events_from(f, prev)
                except Exception:
                    events = []
                done = any(e.get("type") == "assistant" and e.get("stop_reason") == "end_turn"
                           for e in events)
                if not done:
                    continue
                # Preview = the last assistant text in this turn (fallback to title).
                last_text = ""
                for e in events:
                    if e.get("type") == "assistant" and e.get("text"):
                        last_text = e["text"]
                title = claude_storage._ai_title(f) or "Claude"
                # Same-title flood guard (backstop when TAB_FILES is empty): many new
                # session files with an identical title can't all push in 5 min.
                tkey = title.strip().lower()
                now_m = time.monotonic()
                if tkey and now_m - _TITLE_AT.get(tkey, 0.0) < _TITLE_COOLDOWN:
                    continue
                _TITLE_AT[tkey] = now_m
                body = snippet(last_text) if last_text else "Готово ✓"
                _NOTIFIED.add(key)  # one push per unread episode; mark_read() resets it
                # fire-and-forget so a slow send doesn't stall the watch loop.
                # url deep-links to this exact chat (target_id == session uuid).
                asyncio.create_task(send({
                    "title": "💬 " + title[:60],
                    "body": body,
                    "tag": f.stem,
                    "url": "/?open=" + f.stem,
                }))
            first = False
        except Exception:
            pass
        await asyncio.sleep(3)
