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
- Existing Azure resources:
  - A **Foundry** resource + project with a **voice-enabled agent** (see next section).
  - **APIM** (BasicV2 or higher) with a WebSocket API fronting the Voice Live endpoint.
  - This PoC was built against Foundry `foundry-resource-sweden-01` / project `foundry-showcase`
    / agent `voice-mode-agent`, and APIM `apim-ai-gateway-sweden`
    (gateway `wss://apim-ai-gateway-sweden.azure-api.net`).

## Set up the Foundry agent

The UI talks to an existing **prompt agent** in Azure AI Foundry — create one first,
then put its name in `.env` as `AGENT_NAME` (and the project as `AGENT_PROJECT_NAME`).

**Option A — Foundry portal (UI):**
1. Open your project in [Azure AI Foundry](https://ai.azure.com) → **Agents** → **New agent**.
2. Give it a name (e.g. `voice-mode-agent`), pick a chat model (e.g. `gpt-4o` / `gpt-5.4-mini`),
   and add a short system prompt. Optionally enable a tool such as **web search**.
3. Voice Live wraps any prompt agent — no extra "voice" toggle is required; the realtime
   endpoint adds speech-to-text and text-to-speech around the agent.
4. Copy the **agent name** and **project name**.

**Option B — code (Azure AI Projects SDK):**
```python
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

project = AIProjectClient(
    endpoint="https://<your-foundry-resource>.services.ai.azure.com/api/projects/<project>",
    credential=DefaultAzureCredential(),
)
agent = project.agents.create_agent(
    model="gpt-4o",
    name="voice-mode-agent",
    instructions="You are a concise, friendly voice assistant. Keep answers short.",
)
print(agent.name)   # <- put this in AGENT_NAME
```

Then set in `.env`:
```
AGENT_NAME=voice-mode-agent
AGENT_PROJECT_NAME=<your-project-name>
FOUNDRY_WS_HOST=wss://<your-foundry-resource>.services.ai.azure.com
```
`AGENT_NAME` / `AGENT_PROJECT_NAME` are the source of truth for both the direct
scripts and the APIM policy (`infra/policy.xml` rewrites the handshake to these
agent coordinates). Grant APIM's managed identity **Cognitive Services User** on the
Foundry resource so it can mint the Entra token on the gateway hop.

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
| `LOG_ANALYTICS_WORKSPACE_ID` / `APPINSIGHTS_APP_ID` / `APPINSIGHTS_WORKSPACE_ID` | *(optional)* query targets for the diagnostic KQL in [`results/kql/`](results/kql/) — not used at runtime |

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

## Documentation

Deeper topics live in focused docs so this page stays short:

| Doc | Contents |
|-----|----------|
| [docs/benchmarks.md](docs/benchmarks.md) | Latency benchmarks & the "is APIM the bottleneck?" analysis; `bench.py`, `bench_connect.py`, `voice_profile.py`; verify-without-browser scripts. |
| [docs/observability.md](docs/observability.md) | App Insights / OpenTelemetry tracing, per-turn `voice.turn` stage spans, browser mouth-to-ear marks, cross-hop correlation. |
| [results/kql/](results/kql/) | Ready-made KQL for per-turn breakdown, gateway overhead, and joining the hops (+ one-time setup). |

**TL;DR on latency:** APIM is **not** the bottleneck — it adds ~200 ms **once** at connect
and ≈0 per turn; the dominant cost is model generation. See
[docs/benchmarks.md](docs/benchmarks.md) for the numbers.

## Notes / caveats (prototype)

- The APIM subscription key never reaches the browser; the Python backend reads it from
  `.env` (`APIM_SUBSCRIPTION_KEY`). For production, front the backend with your own user
  auth so only signed-in users can open `/realtime`.
- Don't send `instructions`/`voice` overrides in `session.update` for this custom agent —
  the server rejects them and closes the socket. The client only sets modalities/formats.
- `.env` and `.venv/` are git-ignored.

