"""Pure-Python localtunnel client — no Node/npx required.

localtunnel protocol:
  1. GET https://loca.lt/?new  ->  {"id","port","max_conn_count","url"}
  2. Open `max_conn_count` plain-TCP connections to loca.lt:<port>.
  3. The server pairs each public HTTPS request with one of those connections
     and pipes raw bytes; TLS is terminated at loca.lt, so what we forward to
     127.0.0.1:<local_port> is plain HTTP (WebSocket upgrades pass through too).
  4. When a connection closes (request done / idle), reopen it to keep the pool.

This lets ClaudeBridge ship as a single self-contained exe.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
import time
import urllib.request
from urllib.parse import urlparse


def stable_subdomain(token: str) -> str:
    """A fixed, per-install subdomain derived from the auth token, so the public
    URL stays the same across restarts (needed for a usable PWA install / QR)."""
    return "cb-" + hashlib.sha256(token.encode()).hexdigest()[:12]


def request_tunnel(server: str = "https://loca.lt", subdomain: str | None = None) -> dict:
    """Ask the localtunnel server to allocate a tunnel. Returns its JSON."""
    path = "/?new" if not subdomain else "/" + subdomain
    ctx = ssl.create_default_context()
    req = urllib.request.Request(server.rstrip("/") + path, headers={"User-Agent": "claudebridge-lt/1.0"})
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _one_connection(remote_host: str, remote_port: int, local_host: str,
                          local_port: int, health: dict | None = None) -> None:
    """Bridge a single tunnel connection <-> the local service.

    `health["active"]` counts connections that are currently ESTABLISHED (in the
    pipe phase). A 10s connect timeout means a dead loca.lt data port (packets
    stuck in SYN_SENT) fails fast instead of hanging ~20s, so the supervisor can
    notice the pool is empty and grab a fresh tunnel."""
    r_reader, r_writer = await asyncio.wait_for(
        asyncio.open_connection(remote_host, remote_port), timeout=10)
    if health is not None:
        health["active"] = health.get("active", 0) + 1
    try:
        try:
            l_reader, l_writer = await asyncio.open_connection(local_host, local_port)
        except Exception:
            r_writer.close()
            raise
        await asyncio.gather(_pipe(r_reader, l_writer), _pipe(l_reader, r_writer))
    finally:
        if health is not None:
            health["active"] = health.get("active", 1) - 1


async def _request_tunnel_async(server: str, subdomain: str | None) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, request_tunnel, server, subdomain)


async def _acquire(server: str, subdomain: str | None) -> dict:
    """Get a tunnel, preferring the stable subdomain. loca.lt hands out a RANDOM
    subdomain when the requested one is still briefly held (e.g. a just-restarted
    instance whose old tunnel hasn't timed out) — which would change the public
    URL and break the QR/PWA. Retry a few times to reclaim the stable one before
    accepting a random fallback."""
    info = None
    if subdomain:
        for _ in range(6):
            try:
                cand = await _request_tunnel_async(server, subdomain)
            except Exception:
                cand = None
            if cand:
                info = cand  # keep the latest even if it's a fallback
                if (cand.get("id") or "") == subdomain or subdomain in (cand.get("url") or ""):
                    return info  # reclaimed the stable subdomain
            await asyncio.sleep(8)  # let the old tunnel's hold expire, then retry
    if info is None:
        info = await _request_tunnel_async(server, subdomain)
    return info


async def run_tunnel(local_port: int = 8765, server: str = "https://loca.lt",
                     on_url=None, subdomain: str | None = None) -> None:
    """Keep a public tunnel to 127.0.0.1:local_port alive forever.

    Supervisor loop: acquire a tunnel, run its connection pool, and — crucially —
    if the pool can't keep a single live connection (loca.lt sometimes assigns a
    data port that never accepts → connections stall in SYN_SENT → the public URL
    returns 503 forever), drop it and acquire a FRESH tunnel (new port). Calls
    on_url(url) whenever the public URL (re)appears. Runs until cancelled."""
    remote_host = urlparse(server).hostname or "loca.lt"
    last_url = None
    pending = None  # a pre-acquired tunnel to switch to (from background reclaim)
    while True:
        try:
            info = pending or await _acquire(server, subdomain)
        except Exception:
            await asyncio.sleep(3)
            continue
        pending = None
        remote_port = int(info["port"])
        conns = max(1, int(info.get("max_conn_count", 1)))
        url = info["url"]
        # On the stable subdomain? If not, we're on a random fallback (e.g. the
        # stable one was briefly held by a just-killed old instance) and must
        # keep trying to reclaim it in the background so the QR/PWA URL returns.
        on_stable = (not subdomain) or (subdomain in url)
        if on_url and url != last_url:
            last_url = url
            try:
                on_url(url)
            except Exception:
                pass

        health = {"active": 0}
        stop = asyncio.Event()

        async def worker() -> None:
            while not stop.is_set():
                try:
                    await _one_connection(remote_host, remote_port, "127.0.0.1", local_port, health)
                except Exception:
                    await asyncio.sleep(1.0)  # backoff on failure

        workers = [asyncio.create_task(worker()) for _ in range(conns)]
        # Health monitor: if NO connection is established for ~20s, this tunnel's
        # data port is dead — tear it down and loop to acquire a new one.
        zero_since = time.monotonic()
        next_reclaim = time.monotonic() + 25
        try:
            while True:
                await asyncio.sleep(3)
                if health["active"] > 0:
                    zero_since = None
                elif zero_since is None:
                    zero_since = time.monotonic()
                elif time.monotonic() - zero_since > 20:
                    break  # pool has had no live connection for 20s → refresh
                # Background reclaim of the stable subdomain while on a fallback.
                if not on_stable and time.monotonic() >= next_reclaim:
                    next_reclaim = time.monotonic() + 25
                    try:
                        cand = await _request_tunnel_async(server, subdomain)
                    except Exception:
                        cand = None
                    if cand and (subdomain in (cand.get("url") or "")):
                        pending = cand  # reclaimed! switch to it on the next loop
                        break
        finally:
            stop.set()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        await asyncio.sleep(1)  # brief pause before re-acquiring


if __name__ == "__main__":
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    sub = sys.argv[2] if len(sys.argv) > 2 else None

    def show(u):
        print("TUNNEL URL:", u, flush=True)

    try:
        asyncio.run(run_tunnel(port, on_url=show, subdomain=sub))
    except KeyboardInterrupt:
        pass
