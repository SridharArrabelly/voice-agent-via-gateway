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
| 1 | APIM | voice | 1139 | 170 | 1631 | 1679 | 3045 |
| 2 | APIM | text | 1119 | 172 | 1276 | 1457 | 2747 |
| 3 | Direct SDK | voice | 2192 | 223 | 1440 | 1636 | 4158 |
| 4 | Direct SDK | text | 2421 | 199 | 1115 | 1309 | 4410 |

**APIM overhead vs direct SDK (median APIM − median Direct):**

| Modality | connect | session | first | done |
|----------|--------:|--------:|------:|-----:|
| voice | **−1053** | −53 | +191 | +43 |
| text | **−1302** | −27 | +161 | +148 |

**Conclusion — APIM is *not* the bottleneck:**

- **Connect is ~1 s *faster* through APIM.** The raw-WS APIM path authenticates with a
  subscription key and APIM injects a *cached* managed-identity token server-side, while the
  direct SDK pays Entra token acquisition + client init on every connect. So "direct" is not
  automatically faster.
- **The extra proxy hop adds ~+0.2 s to first-token / turn-completion** — a real but modest
  per-turn cost, small next to the model's ~1–1.6 s generation time.
- **End-to-end, APIM is lower** (voice 3045 vs 4158 ms; text 2747 vs 4410 ms)
  because the faster connect offsets the small streaming delta.
- There is **no APIM token/rate-limit policy** throttling the agent path — the `voice-agent`
  API has no limit policy.

**What to tell the customer:** the dominant latency is **model generation**, essentially
identical on both paths. If a perceived slowdown remains, look at (1) reconnecting per turn
instead of reusing a warm WebSocket, and (2) the model **deployment TPM** (gpt-5.4-mini is
at 750k) — not an APIM policy.

> Handshake decomposition (`scripts/bench_connect.py`) confirms this: the ~1s connect is
> mostly network TCP+TLS to the region (paid by the direct path too); APIM adds only
> ~200 ms **one-time** and **≈0 per turn**. Per-turn ttfr is identical with or without APIM.
