# Benchmarks & latency

Scripts and findings that answer the customer's question: **is APIM the bottleneck?**
(Short answer: no — the dominant cost is model generation. Details below.)

## Test scripts

| Script | What it does |
|--------|--------------|
| `scripts/bench.py` | **Voice Live 2×2 latency matrix**: {APIM gateway, direct SDK} × {voice, text}, same agent. Writes `results/bench-matrix.{md,json}`. |
| `scripts/bench_connect.py` | **WebSocket handshake decomposition** (DNS/TCP/TLS/WS-upgrade) + per-turn ttfr: direct-to-Foundry vs via-APIM. Console only. |
| `scripts/voice_profile.py` | **pipeline stage profiler**: feeds a spoken WAV and times ASR / reasoning / tool / TTS per turn (APIM vs direct). |
| `scripts/test_qa.py` | runs a question set through the agent in voice + text; records answers + latency. |

## Verify the path without the browser

```powershell
# through the Python backend (start the backend first):
uv run python scripts/test_proxy.py
# or directly through APIM:
uv run python scripts/test_gateway.py
```

Each prints the agent transcript and the number of audio deltas received.

## Profile where a voice turn's latency goes

`scripts/voice_profile.py` feeds a **spoken question (WAV)** into the agent and reconstructs
the pipeline timeline from the Voice Live event stream, so you can see whether a slow turn is
**ASR**, **model reasoning**, a **web/tool call**, or **TTS** — and how much (if any) the APIM
gateway adds. Meant to be handed to anyone (incl. the customer) to run against their own agent.

```powershell
uv run python scripts/voice_profile.py                    # APIM path, bundled sample WAV
uv run python scripts/voice_profile.py --path direct      # bypass APIM (needs: az login)
uv run python scripts/voice_profile.py --path both --iters 3   # side-by-side + APIM overhead
uv run python scripts/voice_profile.py --wav path\to\your-question.wav   # your own audio
```

- Any mono/stereo 16-bit PCM WAV works (auto-downmixed + resampled to 24 kHz); a sample is
  bundled at `scripts/assets/`. Record a real user question to profile your own traffic.
- Enables `input_audio_transcription` (default model `azure-speech`; override with
  `INPUT_TRANSCRIPTION_MODEL`) so ASR timing is captured.
- Writes `results/voice-profile.{md,json}`. Example finding: even a trivial question
  ("capital of Australia") spends **~1.3 s in ASR** and **~3 s in reasoning (of which ~1.8 s
  is the web tool)**; TTS is fast, and **APIM overhead is ≈0 per stage**.

> For a **live**, correlated version of this same stage breakdown (per turn, in App Insights,
> including browser mouth-to-ear), see [observability.md](observability.md).

## Latency: is APIM the bottleneck?

The customer reported that invoking the **agent** through APIM feels slower than going
direct. `scripts/bench.py` measures this head-on as a **2×2 matrix** against the *same*
agent (`AGENT_NAME`, gpt-5.4-mini, no model sent):

| # | Path | Modality |
|--:|------|----------|
| 1 | APIM gateway (raw WS) | voice |
| 2 | APIM gateway (raw WS) | text |
| 3 | Direct Voice Live **SDK** (Entra) | voice |
| 4 | Direct Voice Live **SDK** (Entra) | text |

Per run it records `connect`, `session`, `first` (first output token), `done`, and `e2e`.
Results are written to `results/bench-matrix.{md,json}`.

```powershell
uv run python scripts/bench.py 6
```

**Findings (median of 6 iterations, ms):**

| # | Path | Modality | connect | session | first | done | e2e |
|--:|------|----------|--------:|--------:|------:|-----:|----:|
| 1 | APIM | voice | 1338 | 160 | 1599 | 1767 | 3342 |
| 2 | APIM | text | 1132 | 131 | 1306 | 1489 | 2791 |
| 3 | Direct SDK | voice | 983 | 207 | 2165 | 2371 | 3574 |
| 4 | Direct SDK | text | 856 | 200 | 1135 | 1355 | 2561 |

**APIM overhead vs direct SDK (median APIM − median Direct):**

| Modality | connect | session | first | done |
|----------|--------:|--------:|------:|-----:|
| voice | **+356** | −46 | −566* | −604* |
| text | **+275** | −69 | +171 | +134 |

<sub>*The voice `first`/`done` deltas are within run-to-run noise (a couple of Direct-voice
iterations had 3–4 s model generations that skew the Direct median upward). Treat per-turn
deltas as ≈0 ± noise — see below.</sub>

> **⚠️ Corrected baseline (2026-07):** earlier runs of this table showed APIM `connect` as
> **~1 s *faster*** than Direct. That was a **measurement artifact**, not reality: the Direct
> SDK path used `AzureCliCredential`, which **shells out to `az` on every `connect()`** to
> fetch an Entra token (1–3 s, sometimes timing out). `bench.py` now **caches the token
> in-process** (representative of a real app using a cached/managed-identity credential), so
> `connect` reflects the actual TLS + WebSocket handshake. Under that apples-to-apples
> comparison APIM is a **small, honest ~0.3 s slower on connect** — the expected cost of a
> second TLS + WS upgrade hop.

**Connect decomposition (where the ~1.3 s goes):**

| Component | ~ms | Reducible? |
|-----------|----:|------------|
| Foundry Voice Live handshake (realtime session alloc + agent load) — the Direct floor | **~0.9 s** | No (inherent to Voice Live) |
| APIM hop (2nd TLS + WS upgrade to Foundry + cached MI token) | **~0.3 s** | Mostly no; already minimal |

**Conclusion — APIM is *not* the bottleneck:**

- **`connect` ≈ 0.9 s Foundry floor + ~0.3 s APIM.** The bulk is Foundry allocating the
  realtime session and loading the agent — paid on the Direct path too. APIM's real cost is a
  one-time ~0.3 s.
- **Per-turn (`first`/`done`) is dominated by model generation** (~1–1.6 s) and is the *same*
  within noise on both paths — the proxy hop adds ≈0 per turn.
- **`connect` is a one-time, per-session cost, not per-turn.** In the browser one WebSocket
  serves the whole conversation; the bench reconnects each iteration (worst case).
- There is **no APIM token/rate-limit policy** throttling the agent path.

**How to make `connect` disappear from perceived latency:**

1. **Reuse one warm socket per session** (never reconnect per turn) — already how the browser
   UI works.
2. **Pre-open the socket on intent.** The browser client (`web/app.js`) **pre-warms** the
   WebSocket on hover/focus/press of *Connect* and starts the mic **in parallel** on the
   click, so the ~1.3 s handshake overlaps the user reaching for the button instead of being
   waited on afterwards.
3. **Tighten the tail, not the median.** Occasional 1.6–3.2 s connects come from **BasicV2 at
   capacity 1** under contention; a second scale unit (or StandardV2) pulls p95 in.
4. Keep client, APIM and Foundry **co-located** (all Sweden Central here).

> Handshake decomposition (`scripts/bench_connect.py`) confirms the split: most of the connect
> is TCP+TLS+session setup paid by the direct path too; APIM adds only ~0.3 s **one-time** and
> **≈0 per turn**.
