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


async def _one_connection(remote_host: str, remote_port: int, local_host: str, local_port: int) -> None:
    """Bridge a single tunnel connection <-> the local service."""
    r_reader, r_writer = await asyncio.open_connection(remote_host, remote_port)
    try:
        l_reader, l_writer = await asyncio.open_connection(local_host, local_port)
    except Exception:
        r_writer.close()
        raise
    await asyncio.gather(_pipe(r_reader, l_writer), _pipe(l_reader, r_writer))


async def run_tunnel(local_port: int = 8765, server: str = "https://loca.lt",
                     on_url=None, subdomain: str | None = None) -> None:
    """Allocate a tunnel and keep its connection pool alive forever.

    Requests `subdomain` if given (falls back to a random one if it's taken).
    Calls on_url(url) once the public URL is known. Reconnects on any drop.
    Runs until cancelled.
    """
    info = request_tunnel(server, subdomain)
    remote_host = urlparse(server).hostname or "loca.lt"
    remote_port = int(info["port"])
    conns = max(1, int(info.get("max_conn_count", 1)))
    url = info["url"]
    if on_url:
        try:
            on_url(url)
        except Exception:
            pass

    async def worker() -> None:
        # Keep one slot of the pool filled: after each request the connection
        # closes, so immediately open a fresh one.
        while True:
            try:
                await _one_connection(remote_host, remote_port, "127.0.0.1", local_port)
            except Exception:
                await asyncio.sleep(1.0)  # backoff on failure

    await asyncio.gather(*[worker() for _ in range(conns)])


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
