# ClaudeBridge

Self-contained Windows/macOS exe that lets a phone (PWA) remotely drive VS Code's
Claude Code chat tabs. Each person runs their own instance — it is NOT a
multi-tenant relay. Distributed as ONE exe via github.com/Junfey/ClaudeBridge.

Never distribute the `.bridge-*` files (token, VAPID keys, push subs, URL) — those
are per-install identity. Only the exe.

## Architecture

- `bridge/main.py` — FastAPI app. REST (`/api/cdp/*`, `/api/version`) + WebSocket
  `/ws/cdp/{target_id}` — the "mirror" loop tails the chat's JSONL and streams
  events (assistant text, tools, working state, question cards) to the phone.
- `bridge/vscode_cdp.py` — drives VS Code over Chrome DevTools Protocol (port
  9222): list windows/tabs, activate a sleeping tab, inject+submit a message,
  answer question cards, read/set mode·model·effort·thinking.
- `bridge/jsonl_watch.py` — tails `~/.claude/projects/<key>/<uuid>.jsonl`.
- `bridge/claude_storage.py` — maps a tab's title → its session JSONL file.
- `bridge/lt_client.py` — pure-Python localtunnel client (public URL, no Node).
- `bridge/push.py` — Web Push (VAPID) so the phone is notified when Claude replies.
- `bridge/version.py` — `VERSION`; also injected into the served HTML.
- `web/index.html` — the ENTIRE PWA (one file: CSS + JS). `web/sw.js` — service worker.
- `launcher_gui.py` — Tkinter window (QR, link, update button). PyInstaller entry point.

## Build & release — follow exactly

1. Bump `VERSION` in `bridge/version.py`.
2. **Kill the running exe FIRST.** On Windows a running exe is locked, so
   PyInstaller exits 0 while silently keeping the OLD binary — you then ship stale
   code and chase ghosts:
   `taskkill //F //IM ClaudeBridge.exe; sleep 2; rm -f dist/ClaudeBridge.exe`
3. `python -m PyInstaller ClaudeBridge.spec --noconfirm --clean`
4. Relaunch `dist/ClaudeBridge.exe`, then **verify the new code is really served**:
   `curl -s http://127.0.0.1:8765/ | grep <a-marker-from-your-change>`
5. `git commit && git tag vX.Y.Z && git push origin main --tags` → CI builds
   Windows + macOS and attaches both to the GitHub Release.

## Hard-won constraints — do not relearn these

- **A session belongs to exactly ONE project. Never look one up unscoped.** Every
  session JSONL carries an authoritative `cwd`; the `~/.claude/projects/<dir>` name
  is only a lossy encoding of it. Always resolve through
  `claude_storage.project_dirs_for_window(window)` + `match_title`, and gate any
  cached mapping with `session_belongs_to_window()`. A global scan once made a tab
  latch onto whatever JSONL was growing at that moment — another project's active
  chat streamed into it, and the wrong mapping got cached permanently.
- **Never do sync file/network I/O on the event loop.** Any blocking call on a hot
  path (mirror, handshake, tab build) freezes every phone's WS heartbeat →
  "связь потеряна" reconnect loops. Wrap it: `await _off(fn, *args)`.
- **loca.lt gives only ~2 concurrent connections**, and the long-lived mirror
  WebSocket holds one for its entire life. `lt_client` therefore keeps a buffer of
  idle tunnel connections; don't shrink it back to `max_conn_count`.
- **`/api/cdp/tabs` is single-flight + 1.5s TTL cached.** It's polled every 3s AND
  on every chat open; concurrent full rebuilds saturate the executor thread pool
  (8 concurrent calls once took 19s). Keep the cache.
- **The installed PWA has NO pull-to-refresh.** Client updates ship via the in-app
  updater: the server injects `__CB_VERSION__` into `index.html` and serves
  `/api/version`; the client compares and offers a reload. To force a refresh of an
  already-installed PWA, fully close it (swipe from recents) and reopen.
- VS Code must be running with `--remote-debugging-port=9222` or the bridge is blind.

## Working style here

Verify behavior, don't assume. There is a real local server on `:8765` and a live
VS Code on `:9222` — probe them (curl, a CDP `Runtime.evaluate`) instead of
guessing. Denis notices missing progress feedback: every async action should show
an immediate optimistic "…" state that resolves when the real result lands.

Deep, non-obvious details — CDP DOM selectors, push/VAPID traps, tunnel
diagnostics, the scroll/badge/status models — live in the auto-memory. Read
`MEMORY.md` and the topic files it links.
