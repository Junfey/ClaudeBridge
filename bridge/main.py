"""FastAPI bridge: REST for windows/sessions, WebSocket for streaming chat."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

UPLOAD_DIR = Path(tempfile.gettempdir()) / "claudebridge-uploads"
MAX_FILES = 10
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB per file — reject bigger, don't hang

from .claude_session import ClaudeSession, new_session, resume_session
from .claude_storage import list_sessions, load_history
from .vscode_windows import list_vscode_windows
from . import vscode_cdp
from . import jsonl_watch
from . import claude_storage
from . import auth
from . import push
from starlette.requests import Request
from starlette.responses import JSONResponse

# In a PyInstaller bundle, data files live under sys._MEIPASS; from source they
# sit next to the package. Resolve both so the exe finds web/index.html.
if getattr(sys, "frozen", False):
    WEB_DIR = Path(sys._MEIPASS) / "web"
else:
    WEB_DIR = Path(__file__).resolve().parent.parent / "web"
LIVE_SESSIONS: dict[str, ClaudeSession] = {}
# target_id -> ws_url, refreshed on each /api/cdp/tabs call
CDP_TABS: dict[str, str] = {}
# target_id -> session title (from the tab header), refreshed on each tabs call
CDP_TAB_TITLES: dict[str, str] = {}
# target_id -> path of the on-disk session file it writes to (learned on send)
CDP_SESSION: dict[str, str] = {}
# session file -> its size the last time you read it on the phone. Used to clear
# the "готово / unread" mark once you've seen it from the app (VS Code only
# clears its own icon when you open the tab on the desktop).
READ_SIZE: dict[str, int] = {}
# session uuid -> unified tab info (rendered + sleeping tabs), built on refresh
CDP_TABS_INFO: dict[str, dict] = {}
# target_id -> asyncio.Lock, so inject / click / read_interactive never run
# concurrently against the same CDP target (Electron rejects that).
_CDP_LOCKS: dict[str, "asyncio.Lock"] = {}
# target_id -> list of client message-ids already injected. Lets the phone
# resend a queued message after a dropped tunnel WITHOUT double-submitting it:
# a resend with a known cid is just re-acked, not re-injected.
CDP_SEEN_CIDS: dict[str, list] = {}


def _cid_is_seen(target_id: str, cid: str | None) -> bool:
    return bool(cid) and cid in CDP_SEEN_CIDS.get(target_id, [])


def _cid_mark(target_id: str, cid: str | None) -> None:
    if not cid:
        return
    lst = CDP_SEEN_CIDS.setdefault(target_id, [])
    lst.append(cid)
    if len(lst) > 100:
        del lst[0 : len(lst) - 100]


def _cdp_lock(target_id: str) -> "asyncio.Lock":
    lock = _CDP_LOCKS.get(target_id)
    if lock is None:
        lock = asyncio.Lock()
        _CDP_LOCKS[target_id] = lock
    return lock


async def _off(fn, *args):
    """Run a BLOCKING function (file stat/read/parse) in a thread so it never
    freezes the event loop — a frozen loop stops every phone's WS heartbeat and
    the tunnel pipes, which was the main cause of the drop/reconnect loops."""
    return await asyncio.get_event_loop().run_in_executor(None, fn, *args)

app = FastAPI(title="ClaudeBridge")


@app.middleware("http")
async def _require_token(request: Request, call_next):
    """Gate /api/* behind the shared token. HTML shell and static stay public
    (the page prompts for the token, then sends it as a header)."""
    path = request.url.path
    if path.startswith("/api/"):
        provided = request.headers.get("x-bridge-key") or request.query_params.get("key")
        if not auth.check(provided):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


class NewSessionReq(BaseModel):
    workspace_path: str


class ResumeSessionReq(BaseModel):
    workspace_path: str
    session_id: str
    title: str = ""


@app.get("/api/windows")
def get_windows() -> list[dict]:
    """List open VS Code workspaces, with count of on-disk sessions per project."""
    windows = list_vscode_windows()
    by_path: dict[str, dict] = {}
    for w in windows:
        if not w.workspace_path:
            continue
        entry = by_path.setdefault(
            w.workspace_path,
            {
                "workspace_path": w.workspace_path,
                "workspace_name": w.workspace_name,
                "window_titles": [],
                "session_count": 0,
            },
        )
        entry["window_titles"].append(w.title)
    for path, entry in by_path.items():
        entry["session_count"] = len(list_sessions(path, limit=200))
    return list(by_path.values())


@app.get("/api/projects/sessions")
def get_project_sessions(path: str = Query(...)) -> list[dict]:
    """List recent on-disk Claude sessions for a project."""
    sessions = list_sessions(path, limit=30)
    return [
        {
            "id": s.id,
            "title": s.title,
            "turns": s.turns,
            "mtime": s.mtime,
            "size_kb": int(s.size / 1024),
            "live": s.id in LIVE_SESSIONS,
        }
        for s in sessions
    ]


@app.get("/api/sessions/{sid}/history")
def get_session_history(sid: str, path: str = Query(...)) -> dict:
    """Load chat history for a session from disk."""
    history = load_history(sid, path)
    return {
        "id": sid,
        "history": [{"role": t.role, "text": t.text, "ts": t.timestamp} for t in history],
    }


@app.post("/api/sessions/new")
def create_new_session(req: NewSessionReq) -> dict:
    if not Path(req.workspace_path).is_dir():
        raise HTTPException(400, "workspace_path not a directory")
    s = new_session(req.workspace_path)
    LIVE_SESSIONS[s.id] = s
    return {"id": s.id, "claude_session_id": s.claude_session_id, "cwd": s.cwd}


@app.post("/api/sessions/resume")
def create_resume_session(req: ResumeSessionReq) -> dict:
    if not Path(req.workspace_path).is_dir():
        raise HTTPException(400, "workspace_path not a directory")
    if req.session_id in LIVE_SESSIONS:
        s = LIVE_SESSIONS[req.session_id]
    else:
        s = resume_session(req.workspace_path, req.session_id, title=req.title)
        LIVE_SESSIONS[s.id] = s
    return {"id": s.id, "claude_session_id": s.claude_session_id, "cwd": s.cwd}


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str) -> dict:
    LIVE_SESSIONS.pop(sid, None)
    return {"ok": True}


@app.websocket("/ws/session/{sid}")
async def ws_session(ws: WebSocket, sid: str) -> None:
    if not auth.check(ws.query_params.get("key")):
        await ws.close(code=4401)
        return
    await ws.accept()
    s = LIVE_SESSIONS.get(sid)
    if not s:
        await ws.send_json({"type": "error", "text": "no such live session — open it first via /api/sessions/resume"})
        await ws.close()
        return
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "text": "bad json"})
                continue
            if msg.get("type") != "send":
                continue
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            try:
                async for evt in s.send(text):
                    await ws.send_json(evt)
                await ws.send_json({"type": "done"})
            except Exception as e:  # noqa: BLE001
                await ws.send_json({"type": "error", "text": str(e)})
    except WebSocketDisconnect:
        return


# ── CDP: live VS Code Claude tabs ──────────────────────────────────────────


@app.get("/api/cdp/available")
def cdp_available() -> dict:
    return {"available": vscode_cdp.cdp_available()}


async def _build_claude_tabs() -> list[dict]:
    """Unified list of ALL open Claude tabs across windows — rendered or not.
    Identity = session uuid (JSONL stem). Rendered tabs get a ws_url for inject;
    sleeping tabs carry their window + label so we can activate them on demand."""
    index = claude_storage.build_title_index()
    info: dict[str, dict] = {}

    def _norm(s: str) -> str:
        return (s or "").strip().rstrip("…").lower()

    def _clean_title(s: str) -> str:
        # With split editor groups VS Code appends ", Editor Group N" to the tab
        # aria-label; strip it so title matching / display stay correct.
        return re.sub(r",\s*(Editor Group|Group)\s*\d+\s*$", "", (s or "")).strip()

    # 1. Every Claude editor tab (rendered or not), identified by the extension's
    #    webview resource marker — NOT by title matching. This lists ALL Claude
    #    tabs regardless of count (6 or 60) or whether their session is recent.
    for wt in await vscode_cdp.list_workbench_tabs():
        if not wt.get("claude"):
            continue
        # Prefer the clean .tab-label text; fall back to (de-suffixed) aria.
        title = (wt.get("label") or "").strip() or _clean_title(wt.get("aria") or "")
        win = wt.get("window") or ""
        sf = claude_storage.match_title(title, index)
        if sf is not None:
            uid = sf.stem
            info.setdefault(uid, {
                "id": uid, "title": title, "session_file": str(sf), "ws_url": None,
                "page_ws_url": wt.get("page_ws_url"), "aria": wt.get("aria") or title,
                "window": win, "rendered": False, "_norm": _norm(title),
                "panel": wt.get("panelId") or "", "done": bool(wt.get("done")),
                "pending": bool(wt.get("pending")),
            })
        else:
            # No recent session file matched (old/untitled/brand-new chat). Still
            # list it; the session file resolves via the DOM when tapped open.
            is_fresh = title == "Claude Code"
            panel = wt.get("panelId") or str(wt.get("index"))
            uid = "tab:" + win + ":" + panel
            info.setdefault(uid, {
                "id": uid, "title": "(новый чат)" if is_fresh else (title or "(новый чат)"),
                "session_file": None, "ws_url": None,
                "page_ws_url": wt.get("page_ws_url"), "aria": wt.get("aria") or title,
                "window": win, "rendered": False, "_norm": _norm(title),
                "panel": wt.get("panelId") or "", "done": bool(wt.get("done")),
                "pending": bool(wt.get("pending")),
            })

    # 2. Rendered webviews → attach ws_url (enables inject/mirror immediately).
    #    Correlate by session file when known, else by normalized title.
    for rt in await vscode_cdp.list_claude_tabs():
        sf = claude_storage.match_title(rt.title, index)
        target = None
        if sf is not None and sf.stem in info:
            target = info[sf.stem]
        else:
            rn = _norm(rt.title)
            for e in info.values():
                if e.get("_norm") and e["_norm"] == rn:
                    target = e
                    break
        if target is not None:
            target["ws_url"] = rt.ws_url
            target["rendered"] = True
            if sf is not None and not target.get("session_file"):
                target["session_file"] = str(sf)

    for e in info.values():
        e.pop("_norm", None)
    CDP_TABS_INFO.clear()
    CDP_TABS_INFO.update(info)
    return list(info.values())


@app.get("/api/cdp/tabs")
async def cdp_tabs() -> dict:
    # cdp=False means VS Code isn't running with the debug port — the phone can't
    # see any tabs then, so tell it that explicitly (not "no tabs found").
    if not await _off(vscode_cdp.cdp_available):
        return {"cdp": False, "tabs": []}
    tabs = await _build_claude_tabs()
    out = []
    now = time.time()
    for t in tabs:
        sf = t.get("session_file")
        # VS Code says this tab is "done/unread". Suppress it once the phone has
        # seen it: you're watching it now, or nothing new arrived since you read.
        app_done = bool(t.get("done"))
        if app_done and sf:
            if sf in push.ACTIVE:
                app_done = False
            else:
                try:
                    sz = os.path.getsize(sf)
                except OSError:
                    sz = 0
                app_done = sz > READ_SIZE.get(sf, 0)
        # "pending" = Claude is waiting for you (question / permission). Takes
        # priority over working/done — it needs an answer.
        pending = bool(t.get("pending"))
        # "working" = the session file was written recently (Claude is generating
        # or running a tool) and it isn't finished/done/waiting. 40s (not 5s) so
        # the "работает…" badge survives a long tool run (tests/installs) where the
        # JSONL is quiet for a while; the done/pending icon clears it authoritatively.
        working = False
        if sf and not app_done and not pending:
            try:
                working = (now - os.path.getmtime(sf) < 40) and not t.get("done")
            except OSError:
                working = False
        out.append({"target_id": t["id"], "title": t["title"], "rendered": t["rendered"],
                    "window": t.get("window", ""), "done": app_done,
                    "working": working, "pending": pending})
    return {"cdp": True, "tabs": out}


async def _ensure_rendered(uid: str) -> tuple[str | None, str | None]:
    """Return (ws_url, session_file) for a tab, activating (rendering) it first
    if it's a sleeping tab so CDP can attach to its webview."""
    info = CDP_TABS_INFO.get(uid)
    if not info:
        await _build_claude_tabs()
        info = CDP_TABS_INFO.get(uid)
    if not info:
        return None, None
    if info.get("ws_url"):
        return info["ws_url"], info.get("session_file")
    # Sleeping tab: click it in VS Code to render its webview, then find it by
    # matching the (now-rendered) webview title to this tab's session/aria.
    # list_claude_tabs() now reads all webview titles CONCURRENTLY, so each poll
    # is ~0.5s instead of ~3s — waking a sleeping tab is quick now.
    if info.get("page_ws_url") and info.get("aria"):
        await vscode_cdp.activate_tab(info["page_ws_url"], info["aria"])
        want_stem = uid if not uid.startswith("tab:") else None
        aria_norm = (info.get("aria") or "").rstrip("…").strip().lower()
        # A brand-new chat is labelled "Claude Code" in the bar but its webview
        # title is "Untitled" — match those explicitly.
        fresh = aria_norm in ("claude code", "(новый чат)", "новый чат", "")
        for _ in range(16):
            index = await _off(claude_storage.build_title_index)
            for rt in await vscode_cdp.list_claude_tabs():
                sf = claude_storage.match_title(rt.title, index)
                tnorm = rt.title.rstrip("…").strip().lower()
                title_match = bool(aria_norm) and tnorm.startswith(aria_norm)
                if fresh and tnorm in ("untitled", "claude code", "(новый чат)"):
                    title_match = True
                if (want_stem and sf is not None and sf.stem == want_stem) or (want_stem is None and title_match):
                    info["ws_url"] = rt.ws_url
                    info["rendered"] = True
                    if sf is not None:
                        info["session_file"] = str(sf)
                    return rt.ws_url, info.get("session_file")
            await asyncio.sleep(0.25)
    return None, info.get("session_file")


@app.websocket("/ws/cdp/{target_id}")
async def ws_cdp(ws: WebSocket, target_id: str) -> None:
    if not auth.check(ws.query_params.get("key")):
        await ws.close(code=4401)
        return
    await ws.accept()
    from pathlib import Path as _P
    session_file = None
    mirror = None
    try:
        # Opening a chat on the phone switches VS Code to that tab (which also
        # clears its "done/unread" badge). SAFE only for ALREADY-rendered tabs —
        # a single activation, no fresh webview to race with. Sleeping tabs are
        # activated once by _ensure_rendered below. (Requiring only ws_url here —
        # not done — restores the "phone switches the VS Code tab" behaviour.)
        _info = CDP_TABS_INFO.get(target_id) or {}
        if _info.get("ws_url") and _info.get("page_ws_url") and _info.get("aria"):
            try:
                await vscode_cdp.activate_tab(_info["page_ws_url"], _info["aria"])
            except Exception:
                pass

        # Bounded so a hard-to-attach tab can NEVER hang the handshake (the phone
        # would show its loader forever).
        try:
            ws_url, session_file_str = await asyncio.wait_for(_ensure_rendered(target_id), timeout=12)
        except asyncio.TimeoutError:
            ws_url, session_file_str = None, None
        if not ws_url:
            await ws.send_json({"type": "history", "messages": []})  # clear the loader
            await ws.send_json({"type": "error", "text": "Не удалось открыть вкладку — открой её в VS Code и зайди снова."})
            await ws.close()
            return
        session_file = _P(session_file_str) if session_file_str else None
        if session_file:
            CDP_SESSION[target_id] = str(session_file)
            push.ACTIVE.add(str(session_file))  # suppress push while you watch this chat
        # NOTE: we deliberately do NOT re-activate the tab here. _ensure_rendered
        # already activates a *sleeping* tab (which clears VS Code's done icon as a
        # side effect). A second activation raced with the just-rendered webview,
        # disposing it → blank chats + accidental split editor groups.

        # Initial history from JSONL (clean) if we know the file, else DOM fallback.
        if session_file is not None:
            events = await _off(jsonl_watch.read_events_from, session_file, 0)
            await ws.send_json({"type": "history", "messages": _events_to_history(events[-60:])})
            cu = await _off(jsonl_watch.context_usage, session_file)
            if cu:
                await ws.send_json({"type": "context", **cu})
        else:
            # New/unmatched tab (no JSONL yet). Always send a history — even an
            # empty one — so the phone's "загружаю чат…" loader clears instead of
            # spinning forever on a brand-new tab.
            msgs = []
            try:
                tr = await asyncio.wait_for(
                    vscode_cdp.read_transcript(ws_url, max_messages=30), timeout=8)
                if tr.get("ok"):
                    msgs = tr.get("messages", [])
            except Exception:
                pass
            await ws.send_json({"type": "history", "messages": msgs})

        # Usage-limit banner (weekly %), if VS Code is showing one.
        try:
            lim = await asyncio.wait_for(vscode_cdp.read_limit(ws_url), timeout=6)
            if lim:
                await ws.send_json({"type": "limit", "text": lim})
        except Exception:
            pass

        # Tell the phone right away whether Claude is mid-turn, so re-entering a
        # working chat (e.g. from the tabs menu) shows the live indicator at once
        # instead of a blank. 45s window (matches the mirror) so a long thinking/
        # tool phase — where the JSONL is quiet for a bit — still reads as working.
        init_working = False
        if session_file is not None:
            try:
                init_working = (time.time() - await _off(os.path.getmtime, str(session_file))) < 45
            except OSError:
                init_working = False
        await ws.send_json({"type": "working", "on": init_working})
        # A pending question card is (re)sent by the mirror within ~0.4s, and the
        # phone restores it instantly from its own state on reconnect + dedupes
        # re-sends — so no flicker without an explicit send here.

        lock = _cdp_lock(target_id)
        # The mirror continuously reflects whatever happens in this tab — live —
        # regardless of who started it, and survives reconnects.
        mirror = asyncio.create_task(_mirror_loop(ws, ws_url, target_id, lock))

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")

            if kind == "send":
                text = (msg.get("text") or "").strip()
                images = msg.get("images") or []
                cid = msg.get("cid")
                if not text and not images:
                    continue
                # Idempotent resend: if the phone already queued this cid and we
                # injected it before (ack lost in the drop), just re-ack — never
                # submit the same task twice.
                if _cid_is_seen(target_id, cid):
                    await ws.send_json({"type": "sent", "text": text, "cid": cid})
                    continue
                async with lock:
                    # Paste image attachments first (best-effort visual attach).
                    for p in images[:MAX_FILES]:
                        try:
                            data = Path(p).read_bytes()
                        except OSError:
                            continue
                        import base64
                        b64 = base64.b64encode(data).decode()
                        mime = "image/png" if p.lower().endswith(".png") else "image/jpeg"
                        await vscode_cdp.paste_image(ws_url, b64, mime, os.path.basename(p))
                        await asyncio.sleep(0.3)
                    inj = await vscode_cdp.inject_and_submit(ws_url, text or "(см. вложения)", submit=True)
                if inj.get("ok"):
                    _cid_mark(target_id, cid)  # mark delivered only after a real submit
                    ack = {"type": "sent", "text": text}
                    if cid:
                        ack["cid"] = cid
                    await ws.send_json(ack)
                    await ws.send_json({"type": "thinking"})
                    await ws.send_json({"type": "working", "on": True})
                else:
                    err = {"type": "error", "text": f"inject failed: {inj.get('error')}"}
                    if cid:
                        err["cid"] = cid  # leave it un-acked so the phone retries
                    await ws.send_json(err)

            elif kind == "answer":
                button = (msg.get("button") or "").strip()
                cid = msg.get("cid")
                if button:
                    async with lock:
                        # Select the option + click "Submit answers" (a question
                        # needs BOTH; a permission just needs the one click).
                        res = await vscode_cdp.answer(ws_url, button)
                    ack = {"type": "answer_result", **res}
                    if cid:
                        ack["cid"] = cid
                    await ws.send_json(ack)
                    if res.get("ok"):
                        await ws.send_json({"type": "thinking"})
                        await ws.send_json({"type": "working", "on": True})

            elif kind == "new_session":
                async with lock:
                    res = await vscode_cdp.click_new_session(ws_url)
                await ws.send_json({"type": "new_session_result", **res})

            elif kind == "ping":
                await ws.send_json({"type": "pong"})  # heartbeat — proves the WS is alive
    except WebSocketDisconnect:
        pass  # client left — normal, don't log a traceback
    except Exception as e:  # noqa: BLE001
        try:
            await ws.send_json({"type": "error", "text": str(e)})
        except Exception:
            pass
    finally:
        if mirror:
            mirror.cancel()
        if session_file:
            push.ACTIVE.discard(str(session_file))
            try:  # mark "read up to here" so the готово mark clears on the phone
                READ_SIZE[str(session_file)] = session_file.stat().st_size
            except OSError:
                pass


def _resolve_session_file(target_id: str, title: str):
    from pathlib import Path as _P
    sess = CDP_SESSION.get(target_id)
    if sess and _P(sess).exists():
        return _P(sess)
    f = claude_storage.find_session_file_by_title(title)
    if f is not None:
        CDP_SESSION[target_id] = str(f)
    return f


def _events_to_history(events: list[dict]) -> list[dict]:
    """Flatten JSONL events into simple {role,text} history rows for the phone."""
    out: list[dict] = []
    for e in events:
        if e["type"] == "user" and e.get("text"):
            out.append({"role": "user", "text": e["text"]})
        elif e["type"] == "assistant":
            for t in e.get("tools", []):
                out.append({"role": "tool", "text": _tool_line(t)})
            if e.get("text"):
                out.append({"role": "assistant", "text": e["text"]})
        elif e["type"] == "tool_result" and e.get("text"):
            out.append({"role": "toolout", "text": e["text"][:500]})
    return out


def _tool_line(t: dict) -> str:
    name = t.get("name", "tool")
    inp = t.get("input", {}) or {}
    detail = ""
    if inp.get("file_path"):
        detail = str(inp["file_path"]).replace("\\", "/").split("/")[-1]
    elif inp.get("command"):
        detail = str(inp["command"])[:60]
    elif inp.get("path"):
        detail = str(inp["path"])
    return f"{name}: {detail}" if detail else name


def _question_card(qinput: dict) -> dict | None:
    """Turn an AskUserQuestion tool input into the phone's interactive-card shape.
    Preserves every question + all its options + descriptions (same as VS Code)."""
    qs = qinput.get("questions") if isinstance(qinput, dict) else None
    if not qs:
        return None
    questions = []
    for q in qs:
        opts = [{"label": o.get("label", ""), "description": o.get("description", "")}
                for o in (q.get("options") or []) if o.get("label")]
        if not opts:
            continue
        questions.append({
            "header": q.get("header", ""),
            "question": q.get("question", ""),
            "multiSelect": bool(q.get("multiSelect")),
            "options": opts,
        })
    if not questions:
        return None
    first = questions[0]
    return {
        "kind": "question",
        "title": first["header"],
        "prompt": first["question"],
        "questions": questions,
        # flat options for the answer path (label match) — all questions' labels
        "options": [{"label": o["label"], "kind": "choice"}
                    for q in questions for o in q["options"]],
    }


def _card_key(card: dict) -> str:
    return (card.get("prompt", "") + "|"
            + "|".join(o["label"] for o in card.get("options", [])))


def _discover_session_file(baseline: dict) -> tuple[str, int] | None:
    """(blocking, run in a thread) Find the chat started in a brand-new tab:
    a JSONL that appeared since `baseline`, else one grown past its baseline."""
    newf = grown = None
    for f in jsonl_watch._all_session_files():
        fp = str(f)
        try:
            sz = f.stat().st_size
        except OSError:
            continue
        base = baseline.get(fp)
        if base is None:
            if sz > 0:
                newf = (fp, 0)
                break
        elif sz > base and grown is None:
            grown = (fp, base)
    return newf or grown


async def _mirror_loop(ws: WebSocket, ws_url: str, target_id: str, lock) -> None:
    """Continuously mirror the tab's live state to the phone: tail the session
    JSONL for new events (from anyone — phone or VS Code) and surface pending
    permission/question cards. Runs until the WebSocket disconnects."""
    import time as _t
    from pathlib import Path as _P

    sess = CDP_SESSION.get(target_id)
    session_file = _P(sess) if sess else None
    pos = await _off(jsonl_watch.file_size, session_file) if session_file else None
    # For a brand-new tab (no session file yet) remember current file sizes, so we
    # can later latch onto the chat the user starts HERE — not replay an unrelated
    # session that just happens to be active elsewhere.
    baseline = await _off(jsonl_watch.snapshot_sizes) if session_file is None else {}
    seen: set[str] = set()
    last_card_key = None
    last_growth = 0.0        # "long ago" so an idle chat isn't reported as working
    last_card_check = 0.0
    turn_done = False        # True once we've seen an end_turn since the last growth
    working_sent = None      # last "working" value pushed to the phone (emit on change)
    pending_card = False     # a question/permission card is currently shown
    no_card_count = 0        # consecutive checks with no card (debounce the clear)

    def _key(evt: dict) -> str:
        if evt["type"] == "assistant":
            return "a:" + evt.get("text", "") + ",".join(
                t.get("name", "") + str(t.get("input")) for t in evt.get("tools", []))
        return evt["type"] + ":" + evt.get("text", "")

    while True:
        try:
            # Discover the session file lazily for a brand-new tab: prefer a file
            # that APPEARED since we started (a fresh chat writes a new JSONL on
            # the first message), else one that grew past its baseline — reading
            # only the new part. Never replays an unrelated already-active chat.
            if session_file is None:
                pick = await _off(_discover_session_file, baseline)
                if pick:
                    session_file = _P(pick[0])
                    CDP_SESSION[target_id] = pick[0]
                    pos = pick[1]

            grew = False
            if session_file is not None:
                size = await _off(jsonl_watch.file_size, session_file)
                if pos is None:
                    pos = size
                if size > pos:
                    events = await _off(jsonl_watch.read_events_from, session_file, pos)
                    pos = size
                    grew = True
                    last_growth = _t.monotonic()
                    for evt in events:
                        k = _key(evt)
                        if k in seen:
                            continue
                        seen.add(k)
                        if evt["type"] == "assistant":
                            if not evt.get("text") and not evt.get("tools"):
                                continue
                            payload = {"type": "assistant", "text": evt.get("text", "")}
                            if evt.get("tools"):
                                payload["tools"] = evt["tools"]
                            await ws.send_json(payload)
                            if evt.get("stop_reason") == "end_turn":
                                turn_done = True
                                await ws.send_json({"type": "done"})
                                cu = await _off(jsonl_watch.context_usage, session_file)
                                if cu:
                                    await ws.send_json({"type": "context", **cu})
                            else:
                                turn_done = False  # still mid-turn (more coming)
                        elif evt["type"] == "tool_result":
                            turn_done = False
                            await ws.send_json({"type": "tool_result", "text": evt.get("text", "")})
                        elif evt["type"] == "user":
                            turn_done = False  # a new prompt landed → working again

            # Persistent "working" heartbeat: on while the file is actively
            # growing and the turn hasn't ended; off on end_turn or after it
            # goes quiet. Emitted only on change so the phone shows a steady
            # "Claude работает…" that survives reconnects.
            # When quiet, check for a pending interactive card (throttled). Prefer
            # the JSONL AskUserQuestion detector (version-independent) and fall
            # back to a DOM scrape for permission prompts.
            quiet_for = _t.monotonic() - last_growth
            if not grew and quiet_for >= 1.0 and (_t.monotonic() - last_card_check) >= 2.0:
                last_card_check = _t.monotonic()
                card = None
                if session_file is not None:
                    try:
                        qi = await _off(jsonl_watch.pending_question, session_file)
                    except Exception:
                        qi = None
                    if qi:
                        card = _question_card(qi)
                if card is None and not lock.locked():
                    async with lock:
                        try:
                            card = await vscode_cdp.read_interactive(ws_url)
                        except Exception:
                            card = None
                pending_card = card is not None
                if card:
                    no_card_count = 0
                    ckey = _card_key(card)
                    if ckey != last_card_key:
                        last_card_key = ckey
                        await ws.send_json({"type": "interactive", "data": card})
                else:
                    # Only clear the card after 2 consecutive empty checks, so a
                    # transient read miss can't blink it. Send an explicit null so
                    # the phone removes it (e.g. the question got answered).
                    no_card_count += 1
                    if last_card_key is not None and no_card_count >= 2:
                        last_card_key = None
                        await ws.send_json({"type": "interactive", "data": None})

            # end_turn (turn_done) is the authoritative "finished" signal; the long
            # timeout is only a fallback for a missed end_turn, so the indicator
            # doesn't wrongly clear during a slow tool run (tests, installs). A
            # pending question means Claude is waiting for YOU, not working.
            now_working = (session_file is not None
                           and (_t.monotonic() - last_growth) < 45.0
                           and not turn_done and not pending_card)
            if now_working != working_sent:
                working_sent = now_working
                await ws.send_json({"type": "working", "on": now_working})

        except asyncio.CancelledError:
            return
        except (WebSocketDisconnect, RuntimeError):
            return  # the WebSocket is gone
        except Exception:
            pass    # transient hiccup (JSONL/CDP) — keep mirroring, don't die
        await asyncio.sleep(0.4)


import shutil
import subprocess


def _code_bin() -> str | None:
    """Path to the REAL Microsoft VS Code — not Cursor or other forks that
    hijack the `code` command on PATH."""
    local = os.environ.get("LOCALAPPDATA", "")
    progf = os.environ.get("PROGRAMFILES", "")
    candidates = [
        Path(local) / "Programs" / "Microsoft VS Code" / "Code.exe",
        Path(local) / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd",
        Path(progf) / "Microsoft VS Code" / "Code.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    # Fallback to PATH only if nothing else (may be a fork like Cursor).
    for c in ("code.cmd", "code"):
        p = shutil.which(c)
        if p:
            return p
    return None


def _open_folder_in_vscode(folder: str) -> None:
    """Open a folder in VS Code. Prefer the `code` CLI (bin/code.cmd) — it opens
    the folder in the RUNNING instance (so the debug port covers it and the tabs
    become visible on the phone). `Code.exe <folder>` alone is unreliable."""
    win_cli = None
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA", "")
        progf = os.environ.get("PROGRAMFILES", "")
        for c in (Path(local) / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd",
                  Path(progf) / "Microsoft VS Code" / "bin" / "code.cmd"):
            if c.exists():
                win_cli = str(c)
                break
        if win_cli:
            subprocess.Popen(["cmd", "/c", win_cli, folder], shell=False)
            return
    code = shutil.which("code") or _code_bin()
    if code:
        subprocess.Popen([code, folder], shell=False)


@app.get("/api/fs/list")
def fs_list(path: str = Query("")) -> dict:
    """List sub-folders for the phone's project browser. Empty path → sensible
    roots (home, common code dirs, drives)."""
    if not path:
        roots = []
        home = Path.home()
        for cand in (home, home / "Documents", Path("C:/Creation"), Path("C:/")):
            if cand.exists():
                roots.append({"name": str(cand), "path": str(cand)})
        return {"path": "", "parent": None, "dirs": roots}
    p = Path(path)
    if not p.is_dir():
        raise HTTPException(400, "not a directory")
    dirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.is_dir() and not child.name.startswith("."):
                dirs.append({"name": child.name, "path": str(child)})
    except PermissionError:
        pass
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs}


class NewTabReq(BaseModel):
    window: str


@app.post("/api/cdp/new-tab")
async def cdp_new_tab(req: NewTabReq) -> dict:
    """Start a new Claude chat in a given VS Code window (project)."""
    # Prefer a rendered tab in that window (no activation needed).
    for info in CDP_TABS_INFO.values():
        if info.get("window") == req.window and info.get("ws_url"):
            return await vscode_cdp.click_new_session(info["ws_url"])
    # Else activate any tab in that window, then start a new session.
    for uid, info in list(CDP_TABS_INFO.items()):
        if info.get("window") == req.window:
            ws_url, _ = await _ensure_rendered(uid)
            if ws_url:
                return await vscode_cdp.click_new_session(ws_url)
    raise HTTPException(404, "no tab in that window")


class CloseTabReq(BaseModel):
    target_id: str
    others: bool = False


@app.post("/api/cdp/close-tab")
async def cdp_close_tab(req: CloseTabReq) -> dict:
    """Close a Claude tab (or all others in its window) from the phone."""
    info = CDP_TABS_INFO.get(req.target_id)
    if not info:
        await _build_claude_tabs()
        info = CDP_TABS_INFO.get(req.target_id)
    if not info or not info.get("page_ws_url"):
        raise HTTPException(404, "tab not found")
    page_ws = info["page_ws_url"]
    if req.others:
        win = info.get("window")
        keep = info.get("panel")
        targets = [
            {"panel": i.get("panel"), "aria": i.get("aria")}
            for i in CDP_TABS_INFO.values()
            if i.get("window") == win and i.get("panel") and i.get("panel") != keep
        ]
    else:
        targets = [{"panel": info.get("panel"), "aria": info.get("aria")}]
    n = await vscode_cdp.close_tabs(page_ws, targets)
    await _build_claude_tabs()  # refresh the cached list
    return {"ok": n > 0, "closed": n}


@app.get("/api/cdp/mode")
async def cdp_get_mode(target_id: str) -> dict:
    ws_url, _ = await _ensure_rendered(target_id)
    if not ws_url:
        raise HTTPException(404, "tab not rendered")
    mode = await vscode_cdp.read_mode(ws_url)
    return {"mode": mode, "options": vscode_cdp.MODE_LABELS}


class SetModeReq(BaseModel):
    target_id: str
    mode: str


@app.post("/api/cdp/mode")
async def cdp_set_mode(req: SetModeReq) -> dict:
    ws_url, _ = await _ensure_rendered(req.target_id)
    if not ws_url:
        raise HTTPException(404, "tab not rendered")
    return await vscode_cdp.set_mode(ws_url, req.mode)


@app.get("/api/cdp/models")
async def cdp_get_models(target_id: str) -> dict:
    ws_url, _ = await _ensure_rendered(target_id)
    if not ws_url:
        raise HTTPException(404, "tab not rendered")
    return await vscode_cdp.get_models(ws_url)


class SetModelReq(BaseModel):
    target_id: str
    model: str


@app.post("/api/cdp/model")
async def cdp_set_model(req: SetModelReq) -> dict:
    ws_url, _ = await _ensure_rendered(req.target_id)
    if not ws_url:
        raise HTTPException(404, "tab not rendered")
    return await vscode_cdp.set_model(ws_url, req.model)


class TargetReq(BaseModel):
    target_id: str


@app.post("/api/cdp/thinking")
async def cdp_toggle_thinking(req: TargetReq) -> dict:
    ws_url, _ = await _ensure_rendered(req.target_id)
    if not ws_url:
        raise HTTPException(404, "tab not rendered")
    return await vscode_cdp.toggle_thinking(ws_url)


@app.get("/api/cdp/controls")
async def cdp_controls(target_id: str) -> dict:
    """Current effort + thinking state for the settings sheet (fast; effort has
    no menu, thinking opens the / menu once)."""
    ws_url, _ = await _ensure_rendered(target_id)
    if not ws_url:
        raise HTTPException(404, "tab not rendered")
    effort = await vscode_cdp.read_effort(ws_url)
    thinking = await vscode_cdp.read_thinking(ws_url)
    return {"effort": effort, "thinking": thinking.get("on", False)}


class SetEffortReq(BaseModel):
    target_id: str
    index: int


@app.post("/api/cdp/effort")
async def cdp_set_effort(req: SetEffortReq) -> dict:
    ws_url, _ = await _ensure_rendered(req.target_id)
    if not ws_url:
        raise HTTPException(404, "tab not rendered")
    return await vscode_cdp.set_effort(ws_url, req.index)


class AnswerPushReq(BaseModel):
    target_id: str
    button: str


@app.post("/api/cdp/answer-push")
async def cdp_answer_push(req: AnswerPushReq) -> dict:
    """Answer a question/permission straight from a push notification action:
    click the option whose text matches `button` in the tab's interactive card."""
    ws_url, _ = await _ensure_rendered(req.target_id)
    if not ws_url:
        raise HTTPException(404, "tab not rendered")
    async with _cdp_lock(req.target_id):
        return await vscode_cdp.answer(ws_url, req.button)


# ── Web Push ────────────────────────────────────────────────────────
@app.on_event("startup")
async def _start_push_watcher() -> None:
    asyncio.create_task(push.watcher())
    asyncio.create_task(_pending_watcher())
    asyncio.create_task(_startup_online_push())


async def _pending_watcher() -> None:
    """Push when a tab starts *waiting for you* — Claude asked a question or wants
    a permission (VS Code's claude-logo-pending icon). Includes the question text
    and, when readable, the answer options as notification action buttons so you
    can answer straight from the notification. Baselines silently on first pass."""
    prev_pending: set[str] = set()
    first = True
    while True:
        try:
            if await _off(vscode_cdp.cdp_available):
                tabs = await _build_claude_tabs()
                now_pending: set[str] = set()
                for t in tabs:
                    if not t.get("pending"):
                        continue
                    uid = t["id"]
                    now_pending.add(uid)
                    if uid in prev_pending or first:
                        continue  # already notified / baseline
                    sf = t.get("session_file")
                    if sf and str(sf) in push.ACTIVE:
                        continue  # you're watching this chat
                    body = ""
                    actions: list[dict] = []
                    opts: dict[str, str] = {}
                    labels: list[str] = []
                    # 1) AskUserQuestion from JSONL (version-independent) → question
                    #    text + options.
                    if sf:
                        try:
                            qi = await _off(jsonl_watch.pending_question, Path(sf))
                        except Exception:
                            qi = None
                        card = _question_card(qi) if qi else None
                        if card:
                            body = card.get("prompt") or (card.get("title") or "")
                            labels = [o["label"] for o in card.get("options", [])]
                    # 2) else a permission dialog from the DOM (rendered tabs).
                    ws_url = t.get("ws_url")
                    if not labels and ws_url:
                        try:
                            async with _cdp_lock(uid):
                                card = await vscode_cdp.read_interactive(ws_url)
                        except Exception:
                            card = None
                        if card:
                            if card.get("prompt"):
                                body = card["prompt"]
                            labels = [o.get("label", "") for o in card.get("options", []) if o.get("label")]
                    for i, lab in enumerate(labels[:2]):  # Chrome shows ≤2 actions
                        aid = f"opt:{i}"
                        actions.append({"action": aid, "title": lab[:36]})
                        opts[aid] = lab
                    # No permission dialog (Claude just asked in text) → use the last
                    # assistant message as the body, so the push shows the question.
                    if not body and sf:
                        try:
                            _p = Path(sf)
                            _sz = await _off(jsonl_watch.file_size, _p)
                            for e in await _off(jsonl_watch.read_events_from, _p, max(0, _sz - 200_000)):
                                if e.get("type") == "assistant" and e.get("text"):
                                    body = e["text"]
                        except Exception:
                            pass
                    if not body:
                        body = "Claude ждёт твоего ответа"
                    payload = {
                        "title": "❓ Claude спрашивает",
                        "body": push.snippet(body, 150),
                        "tag": "pending-" + uid,
                        "url": "/?open=" + uid,
                        "target_id": uid,
                    }
                    if actions:
                        payload["actions"] = actions
                        payload["opts"] = opts
                    asyncio.create_task(push.send(payload))
                prev_pending = now_pending
                first = False
        except Exception:
            pass
        await asyncio.sleep(5)


async def _startup_online_push() -> None:
    # After a reboot + autostart, tell the phone the PC is back. Outbound to the
    # push service — no tunnel needed. No-op if nobody is subscribed.
    await asyncio.sleep(6)
    try:
        await push.send({"title": "ClaudeBridge", "body": "ПК снова на связи ✅",
                         "tag": "cb-online", "url": "/"})
    except Exception:
        pass


@app.get("/api/push/key")
def push_key() -> dict:
    return {"publicKey": push.PUBLIC_KEY}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request) -> dict:
    sub = await request.json()
    push.add_subscription(sub)
    return {"ok": True}


@app.post("/api/push/test")
async def push_test() -> dict:
    n = await push.send({"title": "ClaudeBridge", "body": "Пуш работает ✅", "url": "/"})
    return {"ok": True, "sent": n}


@app.get("/sw.js")
def service_worker() -> FileResponse:
    # Served from root so its scope covers the whole app.
    return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")


class OpenProjectReq(BaseModel):
    path: str


@app.post("/api/project/open")
def project_open(req: OpenProjectReq) -> dict:
    """Open a folder as a VS Code project (reuses the debug-port instance)."""
    code = _code_bin()
    if not code:
        raise HTTPException(500, "code CLI not found")
    if not Path(req.path).is_dir():
        raise HTTPException(400, "folder not found")
    _open_folder_in_vscode(str(req.path))
    return {"ok": True, "path": req.path}


class CreateProjectReq(BaseModel):
    parent: str
    name: str


@app.post("/api/project/create")
def project_create(req: CreateProjectReq) -> dict:
    """Create a new folder and open it in VS Code."""
    code = _code_bin()
    if not code:
        raise HTTPException(500, "code CLI not found")
    parent = Path(req.parent)
    if not parent.is_dir():
        raise HTTPException(400, "parent not found")
    safe = "".join(c for c in req.name if c.isalnum() or c in " ._-()").strip()
    if not safe:
        raise HTTPException(400, "bad name")
    dest = parent / safe
    dest.mkdir(parents=True, exist_ok=True)
    _open_folder_in_vscode(str(dest))
    return {"ok": True, "path": str(dest)}


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)) -> dict:
    """Receive up to 10 files from the phone, save them on the laptop, and
    return their absolute paths so they can be referenced in a Claude message."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[dict] = []
    for f in files[:MAX_FILES]:
        base = os.path.basename(f.filename or "file")
        base = "".join(c for c in base if c.isalnum() or c in "._- ()")[:80] or "file"
        dest = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{base}"
        # Stream to disk with a size cap so a huge file can't blow up memory / hang.
        written = 0
        with dest.open("wb") as out:
            while True:
                chunk = await f.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f"файл {base} больше {MAX_UPLOAD_BYTES // (1<<20)} МБ")
                out.write(chunk)
        saved.append({"name": base, "path": str(dest)})
    return {"files": saved}


@app.get("/api/uploaded/{name}")
def get_uploaded(name: str) -> FileResponse:
    """Serve a file the phone previously uploaded, so images embedded in past
    messages (referenced by their on-disk path) render as pictures on reload
    instead of showing as a raw path. Basename only — no traversal."""
    safe = os.path.basename(name)
    dest = UPLOAD_DIR / safe
    if safe != name or not dest.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(dest)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
