"""Connect-latency decomposition — who eats the ~1s WS handshake?

Splits the WebSocket handshake into DNS -> TCP -> TLS -> WS-upgrade for two paths:

  A. Direct to Foundry Voice Live   (raw WS, pre-acquired Entra token)
  B. Via APIM gateway               (raw WS, subscription key)

The Entra token is acquired ONCE up front, so token-acquisition time is
excluded from every iteration (this is the flaw in bench.py's SDK path, which
re-acquires a token on every connect and so looks ~1s slower than it really is).

It also measures, per path:
  - session : connect-done -> session.created            (Foundry accepts session)
  - ttfr    : response.create -> first token             (PER-TURN latency, live-voice number)
  - turn2   : a SECOND turn on the SAME open socket       (steady-state per-turn cost)

Usage:  uv run python scripts/bench_connect.py [iterations]   # default 6
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import ssl
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import websockets
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

API_VERSION = os.environ.get("VOICELIVE_API_VERSION", "2026-04-10")
FOUNDRY_WS_HOST = os.environ["FOUNDRY_WS_HOST"].strip()
AGENT = os.environ["AGENT_NAME"].strip()
PROJECT = os.environ["AGENT_PROJECT_NAME"].strip()
GATEWAY = os.environ.get("GATEWAY_WS_URL", "wss://apim-ai-gateway-sweden.azure-api.net/voice-agent")
SUBKEY = os.environ["APIM_SUBSCRIPTION_KEY"].strip()

DIRECT_URL = f"{FOUNDRY_WS_HOST}/voice-live/realtime?api-version={API_VERSION}&agent-name={AGENT}&agent-project-name={PROJECT}"
APIM_URL = f"{GATEWAY}?subscription-key={SUBKEY}"
PROMPT = "Say hello in one short sentence."
READ_TIMEOUT = 60


def tcp_tls_probe(url: str) -> dict:
    """Measure DNS + TCP + TLS to the host (no WS), so we can subtract the
    pure network/TLS baseline from the full handshake."""
    parts = urlsplit(url)
    host = parts.hostname
    port = parts.port or 443
    t0 = time.perf_counter()
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    t_dns = time.perf_counter()
    af, socktype, proto, _, sa = infos[0]
    s = socket.socket(af, socktype, proto)
    s.settimeout(20)
    s.connect(sa)
    t_tcp = time.perf_counter()
    ctx = ssl.create_default_context()
    ss = ctx.wrap_socket(s, server_hostname=host)
    t_tls = time.perf_counter()
    ss.close()
    return {
        "dns": (t_dns - t0) * 1000,
        "tcp": (t_tcp - t_dns) * 1000,
        "tls": (t_tls - t_tcp) * 1000,
    }


async def ws_probe(url: str, headers: dict | None) -> dict:
    """Full WS handshake + session ready + two sequential turns on one socket."""
    net = tcp_tls_probe(url)  # separate connection, same host — network baseline

    t0 = time.perf_counter()
    kw = {"max_size": None, "open_timeout": 20}
    if headers:
        kw["additional_headers"] = headers
    async with websockets.connect(url, **kw) as ws:
        t_open = time.perf_counter()  # DNS+TCP+TLS+WS-upgrade(+APIM upstream dial)
        t_created = None
        ttfr = [None, None]
        for turn in (0, 1):
            # wait for session.created only on first turn
            if turn == 0:
                while True:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=READ_TIMEOUT))
                    if m.get("type") == "session.created":
                        t_created = time.perf_counter()
                        await ws.send(json.dumps({"type": "session.update",
                                                  "session": {"modalities": ["text"]}}))
                        break
                    if m.get("type") == "error":
                        raise RuntimeError(json.dumps(m.get("error")))
            await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": PROMPT}]}}))
            t_req = time.perf_counter()
            await ws.send(json.dumps({"type": "response.create"}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=READ_TIMEOUT)
                if isinstance(raw, bytes):
                    continue
                m = json.loads(raw)
                t = m.get("type")
                if t == "response.text.delta" and ttfr[turn] is None:
                    ttfr[turn] = (time.perf_counter() - t_req) * 1000
                elif t == "response.done":
                    break
                elif t == "error":
                    raise RuntimeError(json.dumps(m.get("error")))

    ws_total = (t_open - t0) * 1000
    upgrade = ws_total - (net["dns"] + net["tcp"] + net["tls"])
    return {
        **net,
        "ws_total": ws_total,
        "ws_upgrade": upgrade,               # app-layer: WS upgrade + (APIM) upstream dial
        "session": (t_created - t_open) * 1000 if t_created else float("nan"),
        "ttfr1": ttfr[0] if ttfr[0] else float("nan"),
        "ttfr2": ttfr[1] if ttfr[1] else float("nan"),
    }


def med(vals):
    vals = [v for v in vals if v == v]
    return statistics.median(vals) if vals else float("nan")


async def run(label: str, url: str, headers, iters: int) -> list[dict]:
    rows = []
    for i in range(iters):
        try:
            r = await ws_probe(url, headers)
            rows.append(r)
            print(f"  [{label}] {i+1}/{iters}: dns={r['dns']:.0f} tcp={r['tcp']:.0f} tls={r['tls']:.0f} "
                  f"ws_upgrade={r['ws_upgrade']:.0f} | session={r['session']:.0f} "
                  f"ttfr1={r['ttfr1']:.0f} ttfr2={r['ttfr2']:.0f} (ms)")
        except Exception as exc:
            print(f"  [{label}] {i+1} FAILED: {str(exc)[:160]}")
        await asyncio.sleep(1.0)
    return rows


def table(label: str, rows: list[dict]):
    keys = ("dns", "tcp", "tls", "ws_upgrade", "ws_total", "session", "ttfr1", "ttfr2")
    print(f"\n=== {label}  (n={len(rows)}) — median ms ===")
    for k in keys:
        print(f"  {k:11} {med([r[k] for r in rows]):8.0f}")


async def main():
    iters = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    print("Acquiring Entra token ONCE (excluded from per-iteration timings)...")
    from azure.identity.aio import AzureCliCredential
    cred = AzureCliCredential()
    try:
        tok = (await cred.get_token("https://ai.azure.com/.default")).token
    finally:
        await cred.close()
    direct_headers = {"Authorization": f"Bearer {tok}"}

    print(f"\n########## A. DIRECT to Foundry (raw WS, pre-acquired token) ##########")
    a = await run("direct", DIRECT_URL, direct_headers, iters)
    print(f"\n########## B. VIA APIM gateway (raw WS, subscription key) ##########")
    b = await run("apim", APIM_URL, None, iters)

    table("A. Direct to Foundry", a)
    table("B. Via APIM gateway", b)

    if a and b:
        print("\n=== APIM overhead (B - A, median ms) ===")
        for k in ("dns", "tcp", "tls", "ws_upgrade", "ws_total", "session", "ttfr1", "ttfr2"):
            print(f"  {k:11} {med([r[k] for r in b]) - med([r[k] for r in a]):+8.0f}")


if __name__ == "__main__":
    asyncio.run(main())
