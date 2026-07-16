# KQL queries — end-to-end voice latency diagnostics

These queries let the client see **exactly where a voice turn's time goes** and
**how much of it (if any) is APIM**. Two data sources back them:

| Data source | Table | What it holds |
|-------------|-------|---------------|
| **Application Insights** (backend `voice-agent-backend`) | `dependencies` — spans `voice.session` and `voice.turn` | Per-turn internal timeline: ASR, reasoning, web/MCP tool, first-audio (TTS), tokens, and browser mouth-to-ear. |
| **Log Analytics** (workspace receiving APIM diag logs) | `ApiManagementGatewayLogs` | APIM gateway request timing: `TotalTime`, `BackendTime`, and the derived gateway overhead. |

> **Workspace-based App Insights?** If your App Insights `ingestionMode` is
> `LogAnalytics` (check with `az monitor app-insights component show`), the data
> lives in the backing Log Analytics workspace as **`AppDependencies`** with a
> **`Properties`** column — use those instead of `dependencies` / `customDimensions`.
> Each query notes this variant.

## One-time setup (do this before running the queries)

The queries are environment-specific. Set these in `.env` (see `.env.example`)
and do the one-time Azure wiring:

1. **App Insights** — already set via `APPLICATIONINSIGHTS_CONNECTION_STRING`
   (the backend sends `voice.turn` spans there). Note its **Application ID**
   as `APPINSIGHTS_APP_ID`; if workspace-based, note the backing workspace GUID
   as `APPINSIGHTS_WORKSPACE_ID`.
2. **APIM gateway logs** — create/choose a Log Analytics workspace, then on your
   APIM instance add a **diagnostic setting** that sends
   `ApiManagementGatewayLogs` (**Dedicated** destination) to it. Record the
   workspace `customerId` as `LOG_ANALYTICS_WORKSPACE_ID`.

Then fill the query placeholders from those env vars:

| Placeholder in `.kql` | `.env` value |
|---|---|
| `<appinsights-resource>` | `APPINSIGHTS_APP_ID` (or the App Insights resource id) |
| `<la-workspace>` | `LOG_ANALYTICS_WORKSPACE_ID` (gateway-logs workspace) |

Run from the CLI, e.g.:

```bash
# App Insights (classic schema)
az monitor app-insights query --app $APPINSIGHTS_APP_ID \
  --analytics-query "$(cat results/kql/01-voice-turn-breakdown.kql)"

# Workspace-based App Insights (AppDependencies) or APIM gateway logs
az monitor log-analytics query -w $APPINSIGHTS_WORKSPACE_ID --analytics-query "<query>"
az monitor log-analytics query -w $LOG_ANALYTICS_WORKSPACE_ID \
  --analytics-query "$(cat results/kql/03-apim-gateway-overhead.kql)"
```


## How the timeline is captured

The backend (`backend/app.py`) is a WebSocket proxy. It reconstructs each turn
from the Voice Live event stream and emits a `voice.turn` child span:

```
speech_stopped ──▶ ASR ──▶ response.created ──▶ [web/MCP tool] ──▶ first token ──▶ first audio ──▶ response.done
                └─ asr_ms ─┘                 └─ reasoning_ms ─┘   └ tts_first_ms ┘
                └──────────────────────────── total_ms ─────────────────────────┘
```

The browser also posts `client.*` marks (`speech_stopped`, `first_audio_played`,
`barge_in`) that the backend records as `turn.client_wait_ms` (true mouth-to-ear)
and `turn.barge_in`. These control frames are intercepted server-side and never
reach Voice Live.

## The queries

| File | Where to run | Purpose |
|------|-------------|---------|
| `01-voice-turn-breakdown.kql` | App Insights → Logs | Every turn's stage split (ASR/reasoning/tool/TTS). |
| `02-voice-turn-percentiles.kql` | App Insights → Logs | P50/P90 per stage across many turns. |
| `03-apim-gateway-overhead.kql` | `log-voice-agent-gateway` → Logs | APIM's own added latency (`TotalTime − BackendTime`). |
| `04-join-hops.kql` | either (cross-resource) | Put gateway overhead next to the internal turn timeline on one row. |

## Fastest way to see it visually

App Insights → **Transaction search** → pick a `voice.session` → the end-to-end
transaction view renders the `voice.turn` children as a waterfall, with each
stage as an event. No KQL needed for a quick look; the queries above are for
aggregation and for joining the APIM hop.
