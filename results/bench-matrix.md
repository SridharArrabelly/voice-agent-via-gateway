# Voice Live latency — APIM gateway vs direct SDK (same agent)

- Generated (UTC): `2026-07-16T18:36:16.424979+00:00`  ·  iterations per run: **6**
- Agent: `voice-mode-agent` / project `foundry-showcase` (model gpt-5.4-mini, no model sent)
- APIM api-version `2026-04-10` · SDK api-version `2026-01-01-preview`

All times in ms (median across iterations).

| # | Path | Modality | connect | session | first | done | e2e | n |
|--:|------|----------|--------:|--------:|------:|-----:|----:|--:|
| 1 | APIM gateway | voice | 1138.5 | 170.1 | 1630.8 | 1679.1 | 3044.9 | 6 |
| 2 | APIM gateway | text | 1118.5 | 172.1 | 1276.1 | 1457.4 | 2747.1 | 6 |
| 3 | Direct SDK | voice | 2191.9 | 223.3 | 1440.3 | 1635.9 | 4157.5 | 6 |
| 4 | Direct SDK | text | 2420.7 | 199.2 | 1114.9 | 1309.2 | 4409.8 | 6 |

## APIM overhead vs direct SDK (median APIM − median Direct)

| Modality | connect | session | first | done |
|----------|--------:|--------:|------:|-----:|
| voice | -1053.4 | -53.2 | +190.5 | +43.2 |
| text | -1302.2 | -27.1 | +161.2 | +148.2 |