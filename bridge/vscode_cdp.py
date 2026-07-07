"""Talk to VS Code's Claude extension over Chrome DevTools Protocol.

VS Code must be launched with --remote-debugging-port=9222. Each Claude chat
tab is a nested webview iframe (extensionId=Anthropic.claude-code). We attach
to that iframe's CDP target and:
  - read the transcript (messages, tool-use rows) from the DOM
  - inject text into the message input and submit
  - detect and answer interactive cards (questions, permission prompts)

Selectors are pinned to stable-ish attributes (role, aria-label, data-*)
rather than hashed CSS class suffixes wherever possible. When the extension
updates and these break, this is the file to fix.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from urllib.request import urlopen

import websockets

CDP_HOST = "127.0.0.1"
CDP_PORT = 9222

# The contenteditable message box. aria-label is the stable anchor.
INPUT_SELECTOR = '[role="textbox"][aria-label="Message input"]'


def _http_get(path: str) -> list | dict:
    url = f"http://{CDP_HOST}:{CDP_PORT}{path}"
    with urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())


def cdp_available() -> bool:
    try:
        _http_get("/json/version")
        return True
    except Exception:
        return False


@dataclass
class ClaudeTab:
    target_id: str
    ws_url: str
    title: str  # session title from the tab header
    url: str


class _CDPConn:
    """One-shot CDP websocket connection with request/response + event capture."""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._events: list[dict] = []
        self._reader = None

    async def __aenter__(self):
        self._reader = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *exc):
        if self._reader:
            self._reader.cancel()

    async def _read_loop(self):
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                mid = m.get("id")
                if mid in self._pending:
                    self._pending.pop(mid).set_result(m)
                else:
                    self._events.append(m)
        except Exception:
            pass

    async def call(self, method: str, params: dict | None = None, timeout: float = 15):
        self._id += 1
        fut = asyncio.get_event_loop().create_future()
        self._pending[self._id] = fut
        await self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        r = await asyncio.wait_for(fut, timeout=timeout)
        if "error" in r:
            raise RuntimeError(f"{method} → {r['error']}")
        return r.get("result", {})

    def collect_contexts(self) -> list[dict]:
        return [
            e["params"]["context"]
            for e in self._events
            if e.get("method") == "Runtime.executionContextCreated"
        ]

    async def find_claude_context(self) -> int | None:
        """Return the executionContextId whose document has the message input."""
        for c in self.collect_contexts():
            try:
                r = await self.call(
                    "Runtime.evaluate",
                    {
                        "expression": f"!!document.querySelector({INPUT_SELECTOR!r})",
                        "returnByValue": True,
                        "contextId": c["id"],
                    },
                )
                if r.get("result", {}).get("value") is True:
                    return c["id"]
            except Exception:
                continue
        return None

    async def eval_in(self, ctx_id: int, expression: str):
        r = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "contextId": ctx_id},
        )
        return r.get("result", {}).get("value")


def _iter_claude_iframe_targets() -> list[dict]:
    targets = _http_get("/json/list")
    out = []
    for t in targets:
        if t.get("type") != "iframe":
            continue
        url = t.get("url", "")
        if "claude-code" in url.lower() or "Anthropic" in url:
            out.append(t)
    return out


async def _read_tab_title(ws_url: str) -> tuple[str, str | None]:
    """Open a short-lived connection to read the session title + verify input exists."""
    async with websockets.connect(ws_url, max_size=30_000_000) as ws:
        async with _CDPConn(ws) as conn:
            await conn.call("Runtime.enable")
            await conn.call("Page.enable")
            await asyncio.sleep(0.3)
            ctx = await conn.find_claude_context()
            if ctx is None:
                return "", None
            title = await conn.eval_in(
                ctx,
                r"""(() => {
                    const t = document.querySelector('[class*="titleText"]');
                    return t ? t.textContent.trim() : '';
                })()""",
            )
            return (title or "").strip(), "ok"


async def list_claude_tabs() -> list[ClaudeTab]:
    """Enumerate live Claude chat webviews. Only renders that CDP has attached
    show up — background VS Code windows may not appear until focused."""
    tabs: list[ClaudeTab] = []
    for t in _iter_claude_iframe_targets():
        ws_url = t.get("webSocketDebuggerUrl")
        if not ws_url:
            continue
        try:
            title, ok = await _read_tab_title(ws_url)
            if ok is None:
                continue
        except Exception:
            title = ""
        tabs.append(
            ClaudeTab(target_id=t["id"], ws_url=ws_url, title=title or "(Claude)", url=t.get("url", ""))
        )
    return tabs


_TABBAR_JS = r"""
(() => {
  const out = [];
  document.querySelectorAll('.tab').forEach((tab, i) => {
    const aria = tab.getAttribute('aria-label') || '';
    const labelEl = tab.querySelector('.tab-label');
    const label = (labelEl ? labelEl.textContent : aria).trim();
    // ROBUST Claude signal: the Claude Code extension opens each chat as a
    // webview editor whose resource name is 'webview-claudeVSCodePanel-<uuid>'
    // and whose tab icon is claude-logo.svg. This holds for EVERY Claude tab
    // regardless of title, badge, or how many are open — so we never miss one.
    const res = tab.getAttribute('data-resource-name') || '';
    const ip = tab.querySelector('.monaco-icon-label-iconpath');
    const bg = ip ? (ip.style.backgroundImage || '') : '';
    const marker = res.indexOf('webview-claudeVSCodePanel-');
    const claude = marker !== -1 || /claude-logo/.test(bg);
    const panelId = marker !== -1 ? res.slice(marker + 'webview-claudeVSCodePanel-'.length) : '';
    // VS Code swaps the icon to claude-logo-done.svg when Claude has finished a
    // turn and you haven't opened that tab yet — i.e. "ready / unread". It
    // reverts when you view the tab, so this stays in sync with VS Code.
    const done = /claude-logo-done/.test(bg);
    // claude-logo-pending.svg = Claude is waiting for you (a question / permission
    // prompt) — the "blue dot" state. Needs your answer.
    const pending = /claude-logo-pending/.test(bg);
    out.push({ aria, label, active: tab.classList.contains('active'),
               index: i, claude, panelId, done, pending });
  });
  return JSON.stringify(out);
})()
"""


async def _read_tab_bar(page_ws_url: str) -> list[dict]:
    """Read every open editor tab from a workbench window (rendered or not)."""
    async with websockets.connect(page_ws_url, max_size=30_000_000) as ws:
        async with _CDPConn(ws) as conn:
            await conn.call("Runtime.enable")
            await conn.call("Page.enable")
            await asyncio.sleep(0.3)
            for c in conn.collect_contexts():
                try:
                    data = await conn.eval_in(c["id"], _TABBAR_JS)
                except Exception:
                    continue
                if data:
                    try:
                        parsed = json.loads(data)
                    except Exception:
                        continue
                    if parsed:
                        return parsed
    return []


def _window_name(page_title: str) -> str:
    """Extract the project/window name from a VS Code page title of the form
    'SomeTab - ProjectName - Visual Studio Code'."""
    parts = [p.strip() for p in (page_title or "").split(" - ") if p.strip()]
    if len(parts) >= 2 and "Visual Studio Code" in parts[-1]:
        return parts[-2]
    if len(parts) >= 1:
        return parts[0]
    return ""


async def list_workbench_tabs() -> list[dict]:
    """All open editor tabs across all VS Code windows, tagged with the window
    (project) name and the page ws_url. Includes non-Claude tabs; caller filters."""
    out: list[dict] = []
    for t in _http_get("/json/list"):
        if t.get("type") != "page":
            continue
        page_ws = t.get("webSocketDebuggerUrl")
        if not page_ws:
            continue
        window = _window_name(t.get("title", ""))
        try:
            tabs = await _read_tab_bar(page_ws)
        except Exception:
            continue
        for tab in tabs:
            tab["page_ws_url"] = page_ws
            tab["window"] = window
            out.append(tab)
    return out


async def activate_tab(page_ws_url: str, aria_label: str) -> bool:
    """Click a tab in the workbench so VS Code renders its webview (attaches to
    CDP). Switches the active tab on the desktop — fine for remote use."""
    async with websockets.connect(page_ws_url, max_size=30_000_000) as ws:
        async with _CDPConn(ws) as conn:
            await conn.call("Runtime.enable")
            await conn.call("Page.enable")
            await asyncio.sleep(0.2)
            expr = (
                "(() => { const want = %r;"
                " const tab = [...document.querySelectorAll('.tab')]"
                ".find(t => (t.getAttribute('aria-label')||'') === want"
                " || (t.getAttribute('aria-label')||'').startsWith(want));"
                " if(!tab) return false;"
                " const el = tab.querySelector('.tab-label') || tab;"
                " ['mousedown','mouseup','click'].forEach(type =>"
                " el.dispatchEvent(new MouseEvent(type,{bubbles:true,cancelable:true,view:window})));"
                " return true; })()" % aria_label
            )
            for c in conn.collect_contexts():
                try:
                    ok = await conn.eval_in(c["id"], expr)
                    if ok:
                        return True
                except Exception:
                    continue
    return False


_READ_MODE_JS = r"""
(() => {
  const btn = document.querySelector('.footerButtonPrimary_gGYT1w');
  return btn ? (btn.textContent || '').trim() : '';
})()
"""

# Canonical order matches the VS Code Modes menu.
MODE_LABELS = ["Manual", "Edit automatically", "Plan mode", "Auto mode", "Bypass permissions"]

_OPEN_MODE_MENU_JS = r"""
(() => {
  const b = document.querySelector('.footerButtonPrimary_gGYT1w');
  if (!b) return false;
  ['mousedown','mouseup','click'].forEach(t =>
    b.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
  return true;
})()
"""

def _click_mode_item_js(label: str) -> str:
    return (
        "(() => { const want=%r;"
        " const items=[...document.querySelectorAll('button.menuItemV2_8RAulQ')];"
        " const it=items.find(x=>{const l=x.querySelector('.menuItemLabel_8RAulQ');"
        "   return l && l.textContent.trim()===want;});"
        " if(!it) return 'NOITEM';"
        " ['mousedown','mouseup','click'].forEach(t=>"
        "   it.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));"
        " return 'OK'; })()" % label
    )


async def read_mode(ws_url: str) -> str:
    """Current permission/edit mode, read from the composer's mode button."""
    async with websockets.connect(ws_url, max_size=30_000_000) as ws:
        async with _CDPConn(ws) as conn:
            await conn.call("Runtime.enable")
            await asyncio.sleep(0.15)
            for c in conn.collect_contexts():
                try:
                    r = await conn.eval_in(c["id"], _READ_MODE_JS)
                except Exception:
                    continue
                if r:
                    return str(r).strip()
    return ""


async def set_mode(ws_url: str, label: str) -> dict:
    """Open the Modes menu and pick a specific mode by its label."""
    async with websockets.connect(ws_url, max_size=30_000_000) as ws:
        async with _CDPConn(ws) as conn:
            await conn.call("Runtime.enable")
            await asyncio.sleep(0.15)
            ctxs = conn.collect_contexts()
            opened = False
            for c in ctxs:
                try:
                    if await conn.eval_in(c["id"], _OPEN_MODE_MENU_JS):
                        opened = True
                        break
                except Exception:
                    continue
            if not opened:
                return {"ok": False, "error": "mode button not found"}
            await asyncio.sleep(0.35)
            clicked = None
            js = _click_mode_item_js(label)
            for c in ctxs:
                try:
                    r = await conn.eval_in(c["id"], js)
                except Exception:
                    continue
                if r == "OK":
                    clicked = True
                    break
                if r == "NOITEM":
                    clicked = False
            if not clicked:
                # close the menu we opened
                for c in ctxs:
                    try:
                        await conn.eval_in(c["id"], _READ_MODE_JS)
                    except Exception:
                        pass
                return {"ok": False, "error": "mode not found: " + label}
            await asyncio.sleep(0.3)
            new = ""
            for c in ctxs:
                try:
                    new = await conn.eval_in(c["id"], _READ_MODE_JS)
                except Exception:
                    continue
                if new:
                    break
            return {"ok": True, "mode": str(new).strip()}


_OPEN_CMD_JS = "(()=>{const b=document.querySelector('.menuButton_gGYT1w'); if(!b)return false; ['mousedown','mouseup','click'].forEach(t=>b.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}))); return true;})()"
_ESC_JS = "(()=>{for(let i=0;i<2;i++)document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',keyCode:27,bubbles:true}));return 1;})()"
_HAS_COMPOSER_JS = "(()=>!!document.querySelector('.menuButton_gGYT1w'))()"
# "Switch model…" command item (not "Switch models when a message is flagged").
_READ_SWITCH_JS = ("(()=>{const it=[...document.querySelectorAll('.commandItem_G_S7FQ')]"
                   ".find(x=>{const l=x.querySelector('.commandLabel_G_S7FQ');const t=l?l.textContent.trim():'';"
                   "return t.indexOf('Switch model')===0&&t.indexOf('Switch models')!==0;});"
                   " return it?it.textContent.trim():'';})()")
_CLICK_SWITCH_JS = ("(()=>{const it=[...document.querySelectorAll('.commandItem_G_S7FQ')]"
                    ".find(x=>{const l=x.querySelector('.commandLabel_G_S7FQ');const t=l?l.textContent.trim():'';"
                    "return t.indexOf('Switch model')===0&&t.indexOf('Switch models')!==0;});"
                    " if(!it)return false; ['mousedown','mouseup','click'].forEach(t=>"
                    "it.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}))); return true;})()")
_LIST_MODELS_JS = "(()=>JSON.stringify([...document.querySelectorAll('.modelLabel_G8AMvA')].map(s=>s.textContent.trim())))()"


def _click_model_js(name: str) -> str:
    return ("(()=>{const s=[...document.querySelectorAll('.modelLabel_G8AMvA')]"
            ".find(x=>x.textContent.trim()===%r); if(!s)return false;"
            " const row=s.closest('[class*=modelItem],[class*=menuItem],[role=option],[role=menuitem]')||s.parentElement;"
            " ['mousedown','mouseup','click'].forEach(t=>row.dispatchEvent("
            "new MouseEvent(t,{bubbles:true,cancelable:true,view:window}))); return true;})()" % name)


def _click_cmd_js(label: str) -> str:
    return ("(()=>{const it=[...document.querySelectorAll('.commandItem_G_S7FQ')]"
            ".find(x=>{const l=x.querySelector('.commandLabel_G_S7FQ');return l&&l.textContent.trim()===%r;});"
            " if(!it)return false; ['mousedown','mouseup','click'].forEach(t=>"
            "it.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}))); return true;})()" % label)


def _clean_model(switch_text: str) -> str:
    # "Switch model…Opus" -> "Opus"
    rest = (switch_text or "").replace("Switch model", "", 1)
    return re.sub(r"^[^0-9A-Za-zА-Яа-я(]+", "", rest).strip()


async def _with_composer(ws_url: str, fn):
    """Run fn(conn, ctx_id) pinned to the composer's JS context (where the
    command menu lives). The menu renders async, so fn must sleep between steps."""
    async with websockets.connect(ws_url, max_size=30_000_000) as ws:
        async with _CDPConn(ws) as conn:
            await conn.call("Runtime.enable")
            await asyncio.sleep(0.15)
            cid = None
            for c in conn.collect_contexts():
                try:
                    if await conn.eval_in(c["id"], _HAS_COMPOSER_JS):
                        cid = c["id"]
                        break
                except Exception:
                    continue
            if cid is None:
                return None
            return await fn(conn, cid)


_COUNT_CMD_JS = "(()=>document.querySelectorAll('.commandItem_G_S7FQ').length)()"
_COUNT_MODELS_JS = "(()=>document.querySelectorAll('.modelLabel_G8AMvA').length)()"


async def _wait_count(conn, cid, js, tries: int = 16, delay: float = 0.12):
    """Poll a count-returning JS expr until it's > 0 (menus render async)."""
    for _ in range(tries):
        try:
            if await conn.eval_in(cid, js):
                return True
        except Exception:
            pass
        await asyncio.sleep(delay)
    return False


async def _open_menu(conn, cid) -> bool:
    """Open the / command menu deterministically. The button TOGGLES, so only
    click when the menu isn't already showing; retry a few times for reliability."""
    for _ in range(4):
        if await conn.eval_in(cid, _COUNT_CMD_JS):
            return True
        await conn.eval_in(cid, _OPEN_CMD_JS)
        if await _wait_count(conn, cid, _COUNT_CMD_JS, tries=10, delay=0.12):
            return True
    return False


async def get_models(ws_url: str) -> dict:
    """Current model + the selectable list, read from the command (/) menu."""
    async def fn(conn, cid):
        await _open_menu(conn, cid)
        current = _clean_model(await conn.eval_in(cid, _READ_SWITCH_JS))
        await conn.eval_in(cid, _CLICK_SWITCH_JS)
        await _wait_count(conn, cid, _COUNT_MODELS_JS)
        raw = await conn.eval_in(cid, _LIST_MODELS_JS)
        await conn.eval_in(cid, _ESC_JS)
        try:
            opts = json.loads(raw) if raw else []
        except Exception:
            opts = []
        return {"current": current, "options": opts}
    return await _with_composer(ws_url, fn) or {"current": "", "options": []}


async def set_model(ws_url: str, name: str) -> dict:
    """Open the / menu → Switch model → pick `name`. Returns the new model."""
    async def fn(conn, cid):
        await _open_menu(conn, cid)
        await conn.eval_in(cid, _CLICK_SWITCH_JS)
        await _wait_count(conn, cid, _COUNT_MODELS_JS)
        ok = await conn.eval_in(cid, _click_model_js(name))
        await asyncio.sleep(0.4)
        # menu closes on select; reopen to read back the current model
        await _open_menu(conn, cid)
        newv = _clean_model(await conn.eval_in(cid, _READ_SWITCH_JS))
        await conn.eval_in(cid, _ESC_JS)
        return {"ok": bool(ok), "model": newv}
    return await _with_composer(ws_url, fn) or {"ok": False, "error": "composer not found"}


async def toggle_thinking(ws_url: str) -> dict:
    """Toggle the Thinking switch in the / menu; return the new on/off state."""
    async def fn(conn, cid):
        await _open_menu(conn, cid)
        ok = await conn.eval_in(cid, _click_cmd_js("Thinking"))
        await asyncio.sleep(0.3)
        on = await conn.eval_in(cid, _READ_THINKING_JS)
        await conn.eval_in(cid, _ESC_JS)
        return {"ok": bool(ok), "on": bool(on)}
    return await _with_composer(ws_url, fn) or {"ok": False}


# Thinking toggle lives in the / menu; trackOn_0c4GDA class = enabled.
_READ_THINKING_JS = ("(()=>{const it=[...document.querySelectorAll('.commandItem_G_S7FQ')]"
                     ".find(x=>{const l=x.querySelector('.commandLabel_G_S7FQ');return l&&l.textContent.trim()==='Thinking';});"
                     " if(!it)return null; return /trackOn/.test(it.innerHTML);})()")


async def read_thinking(ws_url: str) -> dict:
    async def fn(conn, cid):
        await _open_menu(conn, cid)
        on = await conn.eval_in(cid, _READ_THINKING_JS)
        await conn.eval_in(cid, _ESC_JS)
        return {"on": bool(on)}
    return await _with_composer(ws_url, fn) or {"on": False}


# Effort is a 5-notch slider (.toggle_P1HaRA) in the composer footer — no menu.
_READ_EFFORT_JS = r"""
(() => {
  const tog = document.querySelector('.toggle_P1HaRA');
  if (!tog) return '';
  const notches = [...tog.querySelectorAll('.notch_P1HaRA')];
  const fac = el => { const m = ((el && el.style.width || el && el.style.left) || '').match(/([0-9.]+)\s*\*\s*\(100%/); return m ? parseFloat(m[1]) : 0; };
  const fill = tog.querySelector('.fill_P1HaRA');
  const cur = fac(fill);
  let idx = 0, best = 1e9;
  notches.forEach((n, i) => { const d = Math.abs(fac(n) - cur); if (d < best) { best = d; idx = i; } });
  return JSON.stringify({ index: idx, count: notches.length });
})()
"""


def _set_effort_js(index: int) -> str:
    # The slider reads clientX from the pointer event, so we must aim a full
    # pointer gesture at the target notch's screen x (a plain click won't do).
    return ("(()=>{const tog=document.querySelector('.toggle_P1HaRA'); if(!tog)return false;"
            " const ns=[...tog.querySelectorAll('.notch_P1HaRA')]; const i=%d;"
            " if(i<0||i>=ns.length)return false;"
            " const nr=ns[i].getBoundingClientRect(), r=tog.getBoundingClientRect();"
            " const x=nr.left+nr.width/2, y=r.top+r.height/2;"
            " const o={bubbles:true,cancelable:true,view:window,clientX:x,clientY:y,pointerId:1,isPrimary:true,button:0,buttons:1};"
            " tog.dispatchEvent(new PointerEvent('pointerdown',o));"
            " tog.dispatchEvent(new PointerEvent('pointermove',o));"
            " tog.dispatchEvent(new PointerEvent('pointerup',o));"
            " tog.dispatchEvent(new MouseEvent('click',o));"
            " return true;})()" % index)


_READ_LIMIT_JS = ("(()=>{const b=document.querySelector('.banner_88YE4g');"
                  " if(!b)return ''; return (b.textContent||'').replace(/View usage.*$/i,'').trim();})()")


async def read_limit(ws_url: str) -> str:
    """Read the 'You've used X% of your weekly limit' banner, if shown."""
    async with websockets.connect(ws_url, max_size=30_000_000) as ws:
        async with _CDPConn(ws) as conn:
            await conn.call("Runtime.enable")
            await asyncio.sleep(0.1)
            for c in conn.collect_contexts():
                try:
                    r = await conn.eval_in(c["id"], _READ_LIMIT_JS)
                except Exception:
                    continue
                if r:
                    return str(r).strip()
    return ""


# The Effort slider moved INTO the / command menu (ext 2.1.x) — it's no longer
# in the footer, so it must be opened first, like the model/thinking controls.
async def read_effort(ws_url: str) -> dict:
    async def fn(conn, cid):
        if not await _open_menu(conn, cid):
            return {"index": 0, "count": 0}
        r = await conn.eval_in(cid, _READ_EFFORT_JS)
        await conn.eval_in(cid, _ESC_JS)
        if r:
            try:
                return json.loads(r)
            except Exception:
                pass
        return {"index": 0, "count": 0}
    return await _with_composer(ws_url, fn) or {"index": 0, "count": 0}


async def set_effort(ws_url: str, index: int) -> dict:
    async def fn(conn, cid):
        if not await _open_menu(conn, cid):
            return {"ok": False, "index": 0, "count": 0}
        ok = bool(await conn.eval_in(cid, _set_effort_js(index)))
        await asyncio.sleep(0.25)
        new = {"index": index, "count": 0}
        r = await conn.eval_in(cid, _READ_EFFORT_JS)
        if r:
            try:
                new = json.loads(r)
            except Exception:
                pass
        await conn.eval_in(cid, _ESC_JS)
        return {"ok": ok, **new}
    return await _with_composer(ws_url, fn) or {"ok": False, "index": 0, "count": 0}


_CLOSE_ONE_JS = r"""
(() => {
  const pid = %s, aria = %s;
  const tabs = [...document.querySelectorAll('.tab')];
  let tab = null;
  if (pid) tab = tabs.find(t => (t.getAttribute('data-resource-name')||'').indexOf(pid) !== -1);
  if (!tab && aria) tab = tabs.find(t => (t.getAttribute('aria-label')||'') === aria);
  if (!tab) return false;
  const btn = tab.querySelector('.action-label.codicon-close')
           || tab.querySelector('[class*="codicon-close"]');
  if (btn) {
    ['mousedown','mouseup','click'].forEach(type =>
      btn.dispatchEvent(new MouseEvent(type,{bubbles:true,cancelable:true,view:window})));
    return true;
  }
  // Fallback: middle-click the tab (VS Code closes on mouse button 1/middle).
  const el = tab.querySelector('.tab-label') || tab;
  ['mousedown','mouseup'].forEach(type =>
    el.dispatchEvent(new MouseEvent(type,{bubbles:true,cancelable:true,view:window,button:1})));
  return true;
})()
"""


async def close_tabs(page_ws_url: str, targets: list[dict]) -> int:
    """Close one or more editor tabs in a workbench window. Each target is
    {'panel': <resource-id>, 'aria': <label>} — panel id preferred (unique)."""
    if not targets:
        return 0
    closed = 0
    async with websockets.connect(page_ws_url, max_size=30_000_000) as ws:
        async with _CDPConn(ws) as conn:
            await conn.call("Runtime.enable")
            await conn.call("Page.enable")
            await asyncio.sleep(0.2)
            ctxs = conn.collect_contexts()
            for tgt in targets:
                js = _CLOSE_ONE_JS % (
                    json.dumps(tgt.get("panel") or ""),
                    json.dumps(tgt.get("aria") or ""),
                )
                for c in ctxs:
                    try:
                        ok = await conn.eval_in(c["id"], js)
                    except Exception:
                        continue
                    if ok:
                        closed += 1
                        break
                await asyncio.sleep(0.25)  # let VS Code settle between closes
    return closed


async def inject_and_submit(ws_url: str, text: str, submit: bool = True) -> dict:
    """Focus the input, insert text, optionally press Enter to send."""
    async with websockets.connect(ws_url, max_size=30_000_000) as ws:
        async with _CDPConn(ws) as conn:
            await conn.call("Runtime.enable")
            await conn.call("Page.enable")
            await conn.call("Input.setIgnoreInputEvents", {"ignore": False})
            await asyncio.sleep(0.25)
            ctx = await conn.find_claude_context()
            if ctx is None:
                return {"ok": False, "error": "input not found"}

            focus = await conn.eval_in(
                ctx,
                rf"""(() => {{
                    const el = document.querySelector({INPUT_SELECTOR!r});
                    if (!el) return {{ok:false}};
                    el.focus();
                    const r = document.createRange();
                    r.selectNodeContents(el);
                    r.collapse(false);
                    const s = window.getSelection();
                    s.removeAllRanges();
                    s.addRange(r);
                    return {{ok: document.activeElement === el}};
                }})()""",
            )
            if not focus or not focus.get("ok"):
                return {"ok": False, "error": "focus failed"}

            await asyncio.sleep(0.15)
            await conn.call("Input.insertText", {"text": text})
            await asyncio.sleep(0.35)

            landed = await conn.eval_in(
                ctx,
                f"document.querySelector({INPUT_SELECTOR!r}).textContent",
            )
            if not submit:
                return {"ok": True, "text": landed, "submitted": False}

            for typ in ("keyDown", "keyUp"):
                await conn.call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": typ,
                        "windowsVirtualKeyCode": 13,
                        "nativeVirtualKeyCode": 13,
                        "key": "Enter",
                        "code": "Enter",
                    },
                )
                await asyncio.sleep(0.03)
            return {"ok": True, "text": landed, "submitted": True}


async def paste_image(ws_url: str, b64: str, mime: str, name: str = "image.png") -> dict:
    """Try to paste an image into the Claude input as a real attachment by
    dispatching a synthetic paste event carrying the image file. Whether it
    shows as a thumbnail depends on the extension's paste handler."""
    async with websockets.connect(ws_url, max_size=60_000_000) as ws:
        async with _CDPConn(ws) as conn:
            await conn.call("Runtime.enable")
            await asyncio.sleep(0.2)
            ctx = await conn.find_claude_context()
            if ctx is None:
                return {"ok": False, "error": "not a claude tab"}
            expr = (
                "(() => {"
                "  const b64 = %r, mime = %r, name = %r;"
                "  const bin = atob(b64);"
                "  const arr = new Uint8Array(bin.length);"
                "  for (let i=0;i<bin.length;i++) arr[i]=bin.charCodeAt(i);"
                "  const file = new File([arr], name, {type: mime});"
                "  const dt = new DataTransfer(); dt.items.add(file);"
                "  const el = document.querySelector(%r);"
                "  if(!el) return {ok:false, error:'no input'};"
                "  el.focus();"
                "  const evt = new Event('paste', {bubbles:true, cancelable:true});"
                "  Object.defineProperty(evt, 'clipboardData', {value: dt});"
                "  const ok = el.dispatchEvent(evt);"
                "  return {ok:true, dispatched: ok};"
                "})()"
            ) % (b64, mime, name, INPUT_SELECTOR)
            res = await conn.eval_in(ctx, expr)
            return res or {"ok": False}


async def click_new_session(ws_url: str) -> dict:
    """Click the 'New session' button in the Claude tab (starts a fresh chat)."""
    async with websockets.connect(ws_url, max_size=30_000_000) as ws:
        async with _CDPConn(ws) as conn:
            await conn.call("Runtime.enable")
            await asyncio.sleep(0.2)
            ctx = await conn.find_claude_context()
            if ctx is None:
                return {"ok": False, "error": "not a claude tab"}
            res = await conn.eval_in(
                ctx,
                r"""(() => {
                    const b = document.querySelector('[aria-label="New session"], [title="New session"]');
                    if(!b) return {ok:false, error:'button not found'};
                    b.click(); return {ok:true};
                })()""",
            )
            return res or {"ok": False}


async def read_transcript(ws_url: str, max_messages: int = 40) -> dict:
    """Scrape the visible chat transcript from the webview DOM."""
    async with websockets.connect(ws_url, max_size=30_000_000) as ws:
        async with _CDPConn(ws) as conn:
            await conn.call("Runtime.enable")
            await conn.call("Page.enable")
            await asyncio.sleep(0.3)
            ctx = await conn.find_claude_context()
            if ctx is None:
                return {"ok": False, "error": "not a claude tab"}
            data = await conn.eval_in(ctx, _TRANSCRIPT_JS.replace("__MAX__", str(max_messages)))
            try:
                return {"ok": True, **json.loads(data)}
            except Exception:
                return {"ok": False, "error": "parse failed", "raw": str(data)[:300]}


# JS that walks the Claude transcript DOM and returns a structured snapshot.
# Heuristic + resilient: we look for message rows, tool rows, and interactive
# cards (questions / permission prompts) by role and text, not fragile classes.
_TRANSCRIPT_JS = r"""
(() => {
  const out = { messages: [], interactive: null, busy: false };

  // Busy indicator: a "stop"/"thinking" affordance or spinner near the input.
  const stopBtn = document.querySelector('[aria-label*="Stop" i], [aria-label*="Cancel" i]');
  out.busy = !!stopBtn;

  // Transcript container: the scrollable region holding messages.
  // Claude uses a list of message blocks; grab top-level children with text.
  const scrollHost = document.querySelector('[class*="conversation"], [class*="messages"], [class*="transcript"], main') || document.body;
  const blocks = [...scrollHost.querySelectorAll('[class*="message"], [class*="turn"]')]
    .filter(el => (el.textContent || '').trim().length > 0);

  const seen = new Set();
  for (const el of blocks) {
    const txt = (el.innerText || el.textContent || '').trim();
    if (!txt || seen.has(txt)) continue;
    seen.add(txt);
    const cls = (el.className || '').toString().toLowerCase();
    let role = 'assistant';
    if (cls.includes('user') || el.getAttribute('data-role') === 'user') role = 'user';
    out.messages.push({ role, text: txt.slice(0, 4000) });
  }
  out.messages = out.messages.slice(-__MAX__);

  // Interactive: permission prompt or multiple-choice question.
  // Permission prompts usually contain "Allow"/"Deny"/"Yes"/"No" buttons.
  const buttons = [...document.querySelectorAll('button, [role="button"]')]
    .map(b => ({ el: b, text: (b.textContent || '').trim(), aria: b.getAttribute('aria-label') || '' }))
    .filter(b => b.text || b.aria);

  const allowLike = buttons.filter(b => /^(allow|yes|approve|confirm|да|разрешить)/i.test(b.text));
  const denyLike = buttons.filter(b => /^(deny|no|reject|cancel|нет|запретить|skip)/i.test(b.text));
  if (allowLike.length && denyLike.length) {
    // Find a nearby prompt text / command.
    let prompt = '';
    const region = allowLike[0].el.closest('[class*="permission"], [class*="prompt"], [class*="dialog"], [class*="tool"]') || allowLike[0].el.parentElement;
    if (region) prompt = (region.innerText || '').trim().slice(0, 600);
    out.interactive = {
      kind: 'permission',
      prompt,
      options: [
        ...allowLike.map(b => ({ label: b.text, kind: 'allow' })),
        ...denyLike.map(b => ({ label: b.text, kind: 'deny' })),
      ],
    };
  }

  return JSON.stringify(out);
})()
"""


_INTERACTIVE_JS = r"""
(() => {
  // Strip a leading option number: "1 Yes", "2. Foo", "3) Bar" -> classify text.
  const norm = t => t.replace(/^\s*\d+[\.\)]?\s*/, '').trim();
  const rx = {
    allow: /^(allow|yes|approve|confirm|accept|run|proceed|ok|да|разрешить)\b/i,
    always: /(always|don'?t ask|all future|every time|всегда|не спрашивать)/i,
    deny: /^(deny|no|reject|cancel|skip|stop|decline|нет|запретить|отмена)\b/i,
  };
  const numbered = /^\s*\d+[\.\)]?\s+\S/;  // "N something" — an option button

  const all = [...document.querySelectorAll('button, [role="button"]')]
    .map(b => ({ el: b, text: (b.textContent || '').trim() }))
    .filter(b => b.text && b.text.length < 400);

  // Claude presents choices as a vertical list of numbered buttons. Detect that
  // group generically — works for 2..N options, not just Yes/No.
  let choices = all.filter(b => numbered.test(b.text));

  // Fallback: explicit allow + deny wording even without a numeric prefix.
  if (choices.length < 2) {
    const allow = all.filter(b => rx.allow.test(norm(b.text)));
    const deny = all.filter(b => rx.deny.test(norm(b.text)));
    if (allow.length && deny.length) choices = [...allow, ...deny];
  }
  if (choices.length < 2) return JSON.stringify({ interactive: null });

  const options = choices.map(b => {
    const n = norm(b.text);
    let kind = 'choice';
    if (rx.allow.test(n)) kind = 'allow';
    else if (rx.deny.test(n)) kind = 'deny';
    return { label: b.text, kind, always: rx.always.test(b.text) };
  });

  const hasAllow = options.some(o => o.kind === 'allow');
  const hasDeny = options.some(o => o.kind === 'deny');
  const kind = (hasAllow && hasDeny) ? 'permission' : 'question';

  // Card region + prompt text (question / command being asked about).
  let region = choices[0].el.closest('[class*="permission" i], [class*="prompt" i], [class*="dialog" i], [class*="tool" i], [class*="request" i], [class*="question" i]');
  if (!region) region = choices[0].el.parentElement?.parentElement || choices[0].el.parentElement;
  let prompt = region ? (region.innerText || '').trim() : '';
  // Drop the option lines so the prompt shows just the question/command.
  const optTexts = new Set(options.map(o => o.label));
  prompt = prompt.split('\n').filter(l => !optTexts.has(l.trim())).join('\n').trim().slice(0, 900);

  let title = '';
  const h = region ? region.querySelector('h1,h2,h3') : null;
  if (h) title = (h.textContent || '').trim();

  return JSON.stringify({ interactive: { kind, title, prompt, options } });
})()
"""


async def read_interactive(ws_url: str) -> dict | None:
    """Lightweight poll: return a pending interactive card, or None."""
    try:
        async with websockets.connect(ws_url, max_size=30_000_000) as ws:
            async with _CDPConn(ws) as conn:
                await conn.call("Runtime.enable")
                await asyncio.sleep(0.2)
                ctx = await conn.find_claude_context()
                if ctx is None:
                    return None
                data = await conn.eval_in(ctx, _INTERACTIVE_JS)
                if not data:
                    return None
                parsed = json.loads(data)
                return parsed.get("interactive")
    except Exception:
        return None


async def click_button_by_text(ws_url: str, button_text: str) -> dict:
    """Click a button in the Claude webview matched by (trimmed) text."""
    async with websockets.connect(ws_url, max_size=30_000_000) as ws:
        async with _CDPConn(ws) as conn:
            await conn.call("Runtime.enable")
            await conn.call("Page.enable")
            await asyncio.sleep(0.25)
            ctx = await conn.find_claude_context()
            if ctx is None:
                return {"ok": False, "error": "not a claude tab"}
            res = await conn.eval_in(
                ctx,
                rf"""(() => {{
                    const want = {button_text!r}.trim().toLowerCase();
                    const btns = [...document.querySelectorAll('button, [role="button"]')];
                    const hit = btns.find(b => (b.textContent || '').trim().toLowerCase() === want);
                    if (!hit) return {{ok:false, error:'button not found'}};
                    hit.click();
                    return {{ok:true}};
                }})()""",
            )
            return res or {"ok": False, "error": "no result"}


# Sync helpers for calling from non-async code / quick tests -----------------

def _run(coro):
    return asyncio.run(coro)


if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if not cdp_available():
        print("CDP not available — launch VS Code with --remote-debugging-port=9222")
        raise SystemExit(1)

    tabs = _run(list_claude_tabs())
    print(f"Found {len(tabs)} Claude tab(s):")
    for t in tabs:
        print(f"  [{t.target_id[:12]}] title={t.title!r}")
    if tabs:
        print("\nReading transcript of first tab…")
        tr = _run(read_transcript(tabs[0].ws_url))
        print(f"  ok={tr.get('ok')} busy={tr.get('busy')} messages={len(tr.get('messages', []))}")
        for m in tr.get("messages", [])[-4:]:
            print(f"    [{m['role']}] {m['text'][:80]!r}")
        if tr.get("interactive"):
            print(f"  INTERACTIVE: {tr['interactive']}")
