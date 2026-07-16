// Optional frontend overrides. The browser holds NO secrets.
// By default the client connects same-origin to the Python backend's /realtime WS,
// which owns the APIM subscription key. Set backendWsUrl only to point elsewhere.
window.VOICE_AGENT_CONFIG = {
  // backendWsUrl: "ws://localhost:8000/realtime"
};
