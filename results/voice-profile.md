# Voice Live turn profile — pipeline stage breakdown

- Generated (UTC): `2026-07-16T20:24:04.842110+00:00`
- Agent: `voice-mode-agent` / project `foundry-showcase` · input: `question-capital-australia.wav` (2.6s)
- Stages reconstructed from the Voice Live realtime event stream.

## APIM (median of 3 run(s))

| stage | ms |
|-------|---:|
| user speaking (input audio length) | 2624 |
| ASR / speech-to-text | 955 |
| commit -> model start | 960 |
| reasoning -> first token | 3053 |
|   (of which: web/MCP tool) | 1812 |
| TTS to first audio | 0 |
| TTS playout (speaking) | 203 |
| TOTAL (commit -> done) | 4464 |

- ASR transcript: `What is the capital of Australia?`
- Agent answer: The capital of Australia is **Canberra**.
- Web/MCP tool used: **True**

## DIRECT (median of 3 run(s))

| stage | ms |
|-------|---:|
| user speaking (input audio length) | 2624 |
| ASR / speech-to-text | 1180 |
| commit -> model start | 1187 |
| reasoning -> first token | 3188 |
|   (of which: web/MCP tool) | 1844 |
| TTS to first audio | 0 |
| TTS playout (speaking) | 390 |
| TOTAL (commit -> done) | 5086 |

- ASR transcript: `What is the capital of Australia?`
- Agent answer: The capital of Australia is **Canberra**.
- Web/MCP tool used: **True**

## APIM overhead (median apim − direct, ms)

| stage | Δ ms |
|-------|----:|
| user speaking (input audio length) | +0 |
| ASR / speech-to-text | -225 |
| commit -> model start | -227 |
| reasoning -> first token | -135 |
|   (of which: web/MCP tool) | -32 |
| TTS to first audio | +0 |
| TTS playout (speaking) | -188 |
| TOTAL (commit -> done) | -623 |
