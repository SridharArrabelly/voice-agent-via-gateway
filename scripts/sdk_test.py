import asyncio, os
from pathlib import Path
from dotenv import load_dotenv
from azure.identity.aio import AzureCliCredential
from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    RequestSession, Modality, InputAudioFormat, OutputAudioFormat,
)

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

ENDPOINT = os.environ.get("FOUNDRY_HTTP_HOST") or os.environ["FOUNDRY_WS_HOST"].replace("wss://", "https://")
AGENT = os.environ["AGENT_NAME"]
PROJECT = os.environ["AGENT_PROJECT_NAME"]
SDK_API_VERSION = os.environ.get("VOICELIVE_SDK_API_VERSION", "2026-01-01-preview")

async def main():
    cred = AzureCliCredential()
    print("connecting...")
    async with connect(
        endpoint=ENDPOINT,
        credential=cred,
        api_version=SDK_API_VERSION,
        agent_name=AGENT,
        project_name=PROJECT,
    ) as conn:
        print("CONNECTED OK")
        # request text+audio; do not override voice/instructions (agent-managed)
        session = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
        )
        await conn.session.update(session=session)
        got_audio = 0
        text = ""
        async for evt in conn:
            t = getattr(evt, "type", None)
            ts = str(t)
            if "session.updated" in ts:
                # send a user text turn and ask for a response
                from azure.ai.voicelive.models import UserMessageItem, InputTextContentPart
                await conn.conversation.item.create(item=UserMessageItem(content=[InputTextContentPart(text="Please say hello and tell me in one short sentence what you can do.")]))
                await conn.response.create()
                print("-> sent text turn")
            elif "response.audio_transcript.delta" in ts or "response.text.delta" in ts:
                text += getattr(evt, "delta", "") or ""
            elif "response.audio.delta" in ts:
                got_audio += 1
            elif "response.done" in ts:
                print("RESPONSE DONE. transcript:", text.strip())
                print("audio deltas:", got_audio)
                break
            elif "error" in ts:
                print("ERROR EVT:", getattr(evt, "error", evt))
                break
    await cred.close()
    print("=== SUCCESS ===" if text else "=== no text ===")

asyncio.run(main())
