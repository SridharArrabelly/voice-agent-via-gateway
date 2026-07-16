"""End-to-end test THROUGH the APIM gateway (no local backend).

Connects directly to APIM with the subscription key, runs one text turn, and prints
the agent transcript + audio-delta count. Validates the gateway + managed-identity path.

    python scripts/test_gateway.py
"""
import asyncio
import json
import os
from pathlib import Path

import websockets
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

KEY = os.environ["APIM_SUBSCRIPTION_KEY"].strip()
GATEWAY = os.environ.get("GATEWAY_WS_URL", "wss://apim-ai-gateway-sweden.azure-api.net/voice-agent")
URL = f"{GATEWAY}?subscription-key={KEY}"


async def main() -> int:
    print("Connecting via APIM gateway (no client auth header)...")
    async with websockets.connect(URL, max_size=None) as ws:
        print("OPEN OK (through APIM)")
        text, audio = "", 0
        async for raw in ws:
            m = json.loads(raw)
            t = m.get("type")
            if t == "session.created":
                print("session.created OK -> sending text turn")
                await ws.send(json.dumps({"type": "session.update", "session": {"modalities": ["text", "audio"]}}))
                await ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Say hello and, in one short sentence, what can you do?"}]}}))
                await ws.send(json.dumps({"type": "response.create"}))
            elif t in ("response.audio_transcript.delta", "response.text.delta"):
                text += m.get("delta", "")
            elif t == "response.audio.delta":
                audio += 1
            elif t == "response.done":
                print("TRANSCRIPT:", text.strip())
                print("AUDIO DELTAS:", audio)
                print("=== APIM GATEWAY PATH WORKS ===")
                return 0
            elif t == "error":
                print("ERROR EVT:", json.dumps(m.get("error", m)))
                return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
