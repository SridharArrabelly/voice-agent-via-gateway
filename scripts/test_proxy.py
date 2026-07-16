"""End-to-end test THROUGH the local Python backend proxy.

Requires the backend running (uvicorn backend.app:app). Connects to the browser-facing
WS, runs one text turn, and prints the agent transcript + audio-delta count. Validates:
browser -> Python backend -> APIM -> Foundry (subscription key stays server-side).

    python scripts/test_proxy.py
"""
import asyncio
import json
import os
from pathlib import Path

import websockets
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

URL = os.environ.get("BACKEND_WS_URL", "ws://localhost:8000/realtime")


async def main() -> int:
    print(f"Connecting via Python backend proxy: {URL}")
    async with websockets.connect(URL, max_size=None) as ws:
        print("OPEN OK (through backend, no key sent by client)")
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
                print("=== PYTHON BACKEND PROXY PATH WORKS ===")
                return 0
            elif t == "error":
                print("ERROR EVT:", json.dumps(m.get("error", m)))
                return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
