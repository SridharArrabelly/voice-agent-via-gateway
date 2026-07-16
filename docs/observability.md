# Observability & end-to-end tracing

The backend is instrumented with the **Azure Monitor OpenTelemetry** distro. Set
`APPLICATIONINSIGHTS_CONNECTION_STRING` in `.env` to enable it (leave it unset to run
without tracing). On startup it:

- auto-instruments **FastAPI** (each HTTP request is traced),
- records **one `voice.session` span per WebSocket conversation** through `/realtime`, and
- emits a **`voice.turn` child span per spoken turn** with the full latency breakdown.

On shutdown the backend force-flushes buffered spans, so the last turns of a session are
never lost.

## Session span

| Attribute / event | Meaning |
|---|---|
| `session.id`, `net.peer`, `gateway.url` | session id + browser client + APIM target |
| `upstream.connected` (event, `connect_ms`) | time to establish the APIM/Voice Live WS |
| `voice.session.created` / `voice.response.done` / `voice.*` (events) | key Voice Live server events, incl. transcripts |
| `voice.error` (event, sets span error status) | any server-side Voice Live error |
| `session.duration_ms`, `session.client_messages`, `session.audio_deltas`, `session.server_events` | per-session totals |
| `apim.apim-request-id` / `apim.x-ms-request-id` | APIM handshake request-id — cross-hop join key to `ApiManagementGatewayLogs` |

## Per-turn trace (where the latency actually goes)

For each spoken turn the backend emits a child span **`voice.turn`** under the
session, reconstructed from the Voice Live event stream — the same stage breakdown as
`voice_profile.py`, but live and correlated end-to-end:

| Attribute | Stage |
|---|---|
| `turn.asr_ms` | speech end → transcription done (ASR) |
| `turn.reasoning_ms` | `response.created` → first token (model thinking) |
| `turn.tool_ms` | web / MCP tool call (0 if the agent didn't search) |
| `turn.tts_first_ms` | first token → first audio (TTS start) |
| `turn.total_ms` | speech end → `response.done` |
| `turn.client_wait_ms` | **true browser mouth-to-ear** (from client marks) |
| `turn.barge_in` | user cut in over agent audio |
| `turn.transcript` / `turn.agent_transcript` / `turn.usage.*` | text + token usage |

The browser posts lightweight `client.*` marks (`speech_stopped`, `first_audio_played`,
`barge_in`); the backend intercepts them (they are **never** forwarded to Voice Live) and
records them on the turn. The result is a single App Insights transaction spanning
**browser → backend → APIM → agent → Voice Live → back**. If the Foundry resource also
exports to the same App Insights, its own `invoke_agent`, `chat`, and `execute_tool` spans
appear in the same workspace, so you can see the agent's internal reasoning + tool time too.

### Example (measured)

A real turn for "What is the capital of Australia?":

| asr_ms | reasoning_ms | tts_first_ms | total_ms | client_wait_ms | tokens |
|-------:|-------------:|-------------:|---------:|---------------:|-------:|
| 416 | 830 | 303 | 2142 | 2549 | 1040 |

## Querying it

**One command** (generates turns through the backend, then prints the tables):

```bash
uv run python backend/app.py          # terminal 1: start the proxy
uv run python scripts/trace_report.py --generate 4 --minutes 30   # terminal 2
```

It drives N turns, waits for telemetry export, then prints per-turn `voice.turn` stage
latency (ASR / reasoning / tool / TTS-first / total / mouth-to-ear / tokens) with a median
row, plus the APIM gateway-overhead table. Use `--report-only` to just re-query without
generating new traffic. Reads the workspace IDs from `.env`.

Fastest visual: App Insights → **Transaction search** → pick a `voice.session` → the
end-to-end transaction view renders the `voice.turn` children as a waterfall.

Ready-made KQL lives in [`../results/kql/`](../results/kql/):

- `01` / `02` — per-turn stage split + P50/P90 (App Insights)
- `03` — APIM gateway overhead `TotalTime − BackendTime` (`ApiManagementGatewayLogs`)
- `04` — join the two hops on the APIM request-id (or by time window)

See [`../results/kql/README.md`](../results/kql/README.md) for the one-time setup (App
Insights **Application ID**, the APIM **diagnostic setting** that ships
`ApiManagementGatewayLogs` to a Log Analytics workspace, and the `.env` values that fill the
query placeholders).

> **Workspace-based App Insights?** If `ingestionMode` is `LogAnalytics`, query the backing
> workspace's **`AppDependencies`** table (with the **`Properties`** column) instead of the
> classic `dependencies` / `customDimensions`. Both variants are in the `.kql` files.

## Log-based proof of the APIM tax (for the customer)

To show the customer exactly how much APIM adds, independent of the app:

1. **APIM diagnostic logs → Log Analytics.** `ApiManagementGatewayLogs` records `TotalTime`
   and `BackendTime` per gateway request; `TotalTime − BackendTime` is APIM's own overhead
   (query `03`). For a WebSocket API this is the handshake/upgrade request (the media then
   streams on the open socket).
2. **APIM API Inspector / `<trace>` policy.** Turn on tracing for a single call to see a
   per-policy timing breakdown inside the gateway.
3. **App Insights distributed trace.** The `voice.turn` spans above show the internal split;
   join to the gateway logs via query `04` to put the APIM hop next to them on one row.
