# Voice Live latency — APIM gateway vs direct SDK (same agent)

- Generated (UTC): `2026-07-17T14:10:18.330603+00:00`  ·  iterations per run: **6**
- Agent: `voice-mode-agent` / project `foundry-showcase` (model gpt-5.4-mini, no model sent)
- APIM api-version `2026-04-10` · SDK api-version `2026-01-01-preview`

All times in ms (median across iterations).

| # | Path | Modality | connect | session | first | done | e2e | n |
|--:|------|----------|--------:|--------:|------:|-----:|----:|--:|
| 1 | APIM gateway | voice | 1338.1 | 160.3 | 1599.1 | 1766.5 | 3342.2 | 6 |
| 2 | APIM gateway | text | 1131.8 | 131.2 | 1305.5 | 1489.3 | 2791.3 | 6 |
| 3 | Direct SDK | voice | 982.6 | 206.5 | 2165.2 | 2370.6 | 3573.9 | 6 |
| 4 | Direct SDK | text | 856.4 | 200.1 | 1134.6 | 1354.9 | 2560.7 | 6 |

## APIM overhead vs direct SDK (median APIM − median Direct)

| Modality | connect | session | first | done |
|----------|--------:|--------:|------:|-----:|
| voice | +355.5 | -46.2 | -566.1 | -604.1 |
| text | +275.4 | -68.9 | +170.9 | +134.4 |