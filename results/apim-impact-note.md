# APIM Gateway for Voice Live — what we did & the latency impact

**Repo:** https://github.com/SridharArrabelly/voice-agent-via-gateway
**Date:** 2026-07-16 · **Agent:** `voice-mode-agent` (model gpt-5.4-mini) · project `foundry-showcase`

## What we built (PoC)

A Foundry **Voice Live** agent exposed through an **Azure API Management (APIM) WebSocket
gateway**, called from a browser voice UI via a small Python (FastAPI) WebSocket proxy.

- Browser (mic/audio) → **FastAPI proxy** → **APIM gateway (WSS)** → **Foundry Voice Live agent**.
- The browser holds **no secret**. The APIM subscription key lives only server-side on the proxy.
- APIM authenticates to Foundry using its **managed identity** (token injected by the
  `onHandshake` policy) — no keys to Foundry in the app.
- We invoke the **existing** agent only (no agent created, no model sent). The agent runs its
  own configured model and its web-search tool.

## The question we set out to answer

*"Does putting APIM in front of Voice Live add meaningful latency?"* — and if there's a
slowdown, **which service is actually responsible?**

## Latency recordings

### 1) 2×2 matrix — APIM vs direct SDK, same agent (6 iterations, median ms)

| Path | Modality | connect | first token | end-to-end |
|------|----------|--------:|------------:|-----------:|
| **APIM gateway** | voice | **1139** | 1631 | **3045** |
| **APIM gateway** | text  | **1119** | 1276 | **2747** |
| Direct SDK       | voice | 2192 | 1440 | 4158 |
| Direct SDK       | text  | 2421 | 1115 | 4410 |

**APIM overhead vs direct:** connect **−1053 ms (voice) / −1302 ms (text)** — i.e. APIM was
*faster* to connect (server-side cached managed-identity token vs the SDK acquiring an Entra
token on every connect). First-token +160–190 ms; end-to-end **lower** on APIM.

### 2) Handshake decomposition — where the ~1s connect goes (6 iterations, median ms)

| Phase | Direct→Foundry | Via APIM | Note |
|-------|---------------:|---------:|------|
| DNS | 41 | 48 | cached → ~1 ms |
| TCP connect | 203 | 204 | **network RTT to region** |
| TLS handshake | 426 | 433 | **TLS round-trips to region** |
| WS upgrade | 216 | 438 | APIM adds policy + upstream dial |
| **connect total** | **927** | **1101** | |
| per-turn ttfr (turn 1) | 1191 | 1257 | first token |
| per-turn ttfr (turn 2, same socket) | 1247 | 1189 | steady state |

- **~630 ms of the ~1s connect is pure network TCP+TLS to the region** — paid by the direct
  path too. That's geography (this test ran cross-region to Sweden Central), not APIM.
- **APIM's own added cost is ~+175–220 ms and it is ONE-TIME** (handshake only).
- **Per-turn latency is identical with and without APIM (~1.2 s).** That ~1.2 s is the
  **model generating the first token**, not the gateway.

### 3) End-to-end QA (10 questions, voice + text, through the full browser→proxy→APIM path)

| Modality | connect | time-to-first-response | total | errors |
|----------|--------:|-----------------------:|------:|-------:|
| voice | 2254 ms | 4444 ms | 7538 ms | 0/10 |
| text  | 1385 ms | 3702 ms | 4011 ms | 0/10 |

> The higher voice "total" is **the agent speaking a full multi-sentence answer + a live
> web-search tool call** — not gateway latency, and not silence (the agent starts talking at
> ttfr). Short answers finish in <4 s; two verbose web-search answers (Bitcoin, weather) ran
> 16 s because of answer length, not the gateway.

## Bottom line for the customer

- **APIM is not the bottleneck.** It adds ~200 ms **once** at connect and **≈0 per turn**.
- The latency budget is **(a) network distance to the region** and **(b) model generation**.
- Connect is paid **once per session** (socket stays open) and can be hidden behind the greeting.
- Levers to make live voice snappier, in order of impact: **co-locate region**, **model/tool
  choices** (gate the web-search tool; keep voice answers short), **stream audio on first
  tokens + barge-in**, **reuse a warm WebSocket** (don't reconnect per turn).

## Reproduce

```bash
uv run python scripts/bench.py 6            # 2x2 matrix -> results/bench-matrix.md
uv run python scripts/bench_connect.py 6    # handshake decomposition (console)
uv run python scripts/test_qa.py            # 10-question voice+text -> results/qa-results.md
```
Numbers are region-dependent — run from a machine near the Foundry/APIM region for
representative figures.
