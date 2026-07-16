# Voice Agent via APIM Gateway

A working prototype: a browser voice UI holds a real-time spoken conversation with a
configured Azure **Foundry** agent (`AGENT_NAME` in `.env`). A **Python backend** proxies the browser's
WebSocket to an **Azure API Management (APIM)** WebSocket gateway; APIM injects an
**Entra ID** token (via its **managed identity**) and relays to the Azure **Voice Live**
real-time endpoint. The browser holds no secrets — the APIM subscription key stays on the
Python server.

```
┌──────────┐  PCM16 @24kHz    ┌──────────────────┐  wss + sub-key   ┌──────────────────┐  MI   ┌────────────────────────┐
│ Browser  │  over WS         │  Python backend  │  (server-side)   │  APIM  gateway   │ token │  Azure Voice Live       │
│  mic ────┼─────────────────►│  /realtime proxy ├─────────────────►│  voice-agent(WS) ├──────►│  /voice-live/realtime   │
│  speaker │◄─────────────────┤  (holds sub-key) │◄─────────────────┤  onHandshake pol │       │  agent: $AGENT_NAME     │
└──────────┘  audio deltas    └──────────────────┘                  └──────────────────┘       └────────────────────────┘
```

Why this shape? Browsers can't set an `Authorization` header on a WebSocket, and we don't
want the subscription key in browser code. So the **Python backend** owns the key and
forwards to APIM; **APIM** owns the Entra identity and forwards to Foundry. Each hop holds
exactly the credential it should.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python package manager) and Azure CLI (`az login`) with
  access to the resources below.
- A `.env` file at the project root — copy `.env.example` and fill in values (the
  `APIM_SUBSCRIPTION_KEY` in particular). `.env` is git-ignored.
- Existing Azure resources (already configured in this PoC):
  - Foundry: `foundry-resource-sweden-01`, project `foundry-showcase`, agent `voice-mode-agent`.
  - APIM: `apim-ai-gateway-sweden` (BasicV2), gateway `wss://apim-ai-gateway-sweden.azure-api.net`.

## Configuration (`.env`)

All runtime parameters live in `.env` (loaded via `python-dotenv`). See `.env.example`.

| Variable | Purpose |
|----------|---------|
| `GATEWAY_WS_URL` | APIM WebSocket gateway URL the backend forwards to |
| `APIM_SUBSCRIPTION_KEY` | APIM subscription key (server-side secret; never sent to the browser) |
| `BACKEND_HOST` / `BACKEND_PORT` | address the backend binds to (default `127.0.0.1:8000`) |
| `BACKEND_WS_URL` | target for `scripts/test_proxy.py` |
| `FOUNDRY_WS_HOST`, `AGENT_NAME`, `AGENT_PROJECT_NAME`, `VOICELIVE_API_VERSION` | Foundry agent coordinates (source of truth mirrored in the APIM policy) |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | App Insights connection string; enables OTel tracing (unset = tracing off) |
| `OTEL_SERVICE_NAME` | service name reported to App Insights (default `voice-agent-backend`) |

## Run it

```powershell
cp .env.example .env    # then edit .env and set APIM_SUBSCRIPTION_KEY
uv sync                 # create .venv + install deps (once)
uv run python backend/app.py
# open http://127.0.0.1:8000, click "Start conversation", allow the mic, and talk.
```

The backend serves the frontend and the `/realtime` WebSocket from the same origin, so no
frontend config is needed. The agent replies with voice; transcripts appear in the log.
Turn-taking and barge-in (interrupting the agent) are handled server-side by Voice Live's
semantic VAD.

## How the gateway is wired

APIM exposes a WebSocket API at path `voice-agent`. The policy on the `onHandshake`
operation (`infra/policy.xml`) does two things:

1. `authentication-managed-identity` for `resource="https://ai.azure.com"` → obtains an
   Entra token and sets the `Authorization: Bearer …` header.
2. `rewrite-uri` to the Voice Live path with the agent parameters:
   `/voice-live/realtime?api-version=2026-04-10&agent-name=voice-mode-agent&agent-project-name=foundry-showcase`

The APIM managed identity (`bcae945f-…`) holds **Cognitive Services User** + **Foundry
User** on the Foundry resource.

> **Agent type / api-version:** Voice Live's current contract (`api-version=2026-04-10`)
> requires a **new Foundry agent** — classic (v1) agents are rejected with
> `classic_foundry_agent_not_supported`. The configured agent (`AGENT_NAME`) must be a
> new-style agent to be accepted on `2026-04-10`. (Older classic agents can still be reached
> on `2026-01-01-preview` / `2025-10-01`, but that path is legacy — don't use it for new work.)
>
> To target an agent that lives on a **different** Foundry resource than the gateway's
> backend host, add `&foundry-resource-override=<resource-name>` to the rewrite-uri.

## Layout

| Path | Purpose |
|------|---------|
| `backend/app.py` | FastAPI backend: serves `web/` + proxies `/realtime` WS to APIM (holds sub-key) |
| `backend/telemetry.py` | Application Insights / OpenTelemetry setup (Azure Monitor distro) |
| `pyproject.toml` / `uv.lock` | uv project + pinned dependencies |
| `.env` / `.env.example` | all runtime config (secrets in `.env`, git-ignored; template committed) |
| `web/index.html`, `styles.css` | UI shell |
| `web/app.js` | WebSocket + mic capture (PCM16 @24 kHz) + streaming playback + barge-in |
| `web/pcm-worklet.js` | AudioWorklet that converts mic frames to PCM16 |
| `web/config.js` | optional frontend overrides (no secrets; same-origin by default) |
| `infra/policy.xml` | APIM `onHandshake` policy (MI auth + rewrite-uri) |
| `scripts/test_proxy.py` | end-to-end test **through the Python backend** (text turn) |
| `scripts/test_gateway.py` | end-to-end test **directly through APIM** (text turn) |
| `scripts/sdk_test.py` | Python `azure-ai-voicelive` reference (direct to Voice Live) |
| `scripts/bench.py` | **Voice Live 2×2 latency matrix**: {APIM gateway, direct SDK} × {voice, text}, same agent |
| `scripts/bench_connect.py` | **WebSocket handshake decomposition** (DNS/TCP/TLS/WS-upgrade) + per-turn ttfr: direct-to-Foundry vs via-APIM |
| `scripts/test_qa.py` | runs a question set through the agent in voice + text; records answers + latency |
| `scripts/voice_profile.py` | **pipeline stage profiler**: feeds a spoken WAV and times ASR / reasoning / tool / TTS per turn (APIM vs direct) |

## Verify without the browser

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
  (“capital of Australia”) spends **~1.3 s in ASR** and **~3 s in reasoning (of which ~1.8 s
  is the web tool)**; TTS is fast, and **APIM overhead is ≈0 per stage**.

## Notes / caveats (prototype)

- The APIM subscription key never reaches the browser; the Python backend reads it from
  `.env` (`APIM_SUBSCRIPTION_KEY`). For production, front the backend with your own user
  auth so only signed-in users can open `/realtime`.
- Don't send `instructions`/`voice` overrides in `session.update` for this custom agent —
  the server rejects them and closes the socket. The client only sets modalities/formats.
- `.env` and `.venv/` are git-ignored.

## Observability (Application Insights + OpenTelemetry)

The backend is instrumented with the **Azure Monitor OpenTelemetry** distro. Set
`APPLICATIONINSIGHTS_CONNECTION_STRING` in `.env` to enable it (leave it unset to run
without tracing). On startup it:

- auto-instruments **FastAPI** (each HTTP request is traced), and
- records **one `voice.session` span per WebSocket conversation** through `/realtime`.

Each session span carries:

| Attribute / event | Meaning |
|---|---|
| `net.peer`, `gateway.url` | browser client + APIM target |
| `upstream.connected` (event, `connect_ms`) | time to establish the APIM/Voice Live WS |
| `voice.session.created` / `voice.response.done` / `voice.*` (events) | key Voice Live server events, incl. transcripts |
| `voice.error` (event, sets span error status) | any server-side Voice Live error |
| `session.duration_ms`, `session.client_messages`, `session.audio_deltas`, `session.server_events` | per-session totals |
| `session.closed` (event) | final counters when the socket closes |

View them in the Application Insights resource under **Transaction search** /
**Investigate → Performance**, or query with KQL, e.g.:

```kusto
dependencies
| where name == "voice.session"
| order by timestamp desc
```

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
  API has no limit policy, and the chat API's conditional `llm-token-limit` is **inert**
  (its `tokenlimit-<deployment>` variables are unset; there are zero named values).

**What to tell the customer:** the dominant latency is **model generation**, essentially
identical on both paths. If a perceived slowdown remains, look at (1) reconnecting per turn
instead of reusing a warm WebSocket, and (2) the model **deployment TPM** (gpt-5.4-mini is
at 750k) — not an APIM policy.

> Handshake decomposition (`scripts/bench_connect.py`) confirms this: the ~1s connect is
> mostly network TCP+TLS to the region (paid by the direct path too); APIM adds only
> ~200 ms **one-time** and **≈0 per turn**. Per-turn ttfr is identical with or without APIM.
