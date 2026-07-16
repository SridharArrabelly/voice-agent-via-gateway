/* Voice Agent client
 * Browser mic --(PCM16 @24kHz)--> Python backend (/realtime) --> APIM --> Foundry Voice Live agent
 * The browser holds NO secrets: it connects same-origin to the backend, which owns the
 * APIM subscription key and forwards the stream.
 */
(() => {
  const SAMPLE_RATE = 24000;
  const cfg = window.VOICE_AGENT_CONFIG || {};
  const $ = (id) => document.getElementById(id);
  const logEl = $("log"), statusEl = $("status"), dot = $("dot");
  const connectBtn = $("connectBtn"), stopBtn = $("stopBtn"), micLevel = $("micLevel");
  // Backend WS endpoint: same-origin /realtime by default (backend serves this page).
  const backendWsUrl = cfg.backendWsUrl ||
    `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/realtime`;
  $("gwPill").textContent = "backend: " + backendWsUrl;

  let ws = null, audioCtx = null, workletNode = null, micStream = null, source = null;
  let playCtx = null, playHead = 0, activeSources = [];
  let curAgentMsg = null, curUserMsg = null;
  let turnAudioStarted = false; // reset per agent turn; gates the first-audio mark

  // Post a browser-only timing mark to the backend. These `client.*` frames are
  // intercepted server-side (never forwarded to Voice Live) and recorded on the
  // per-turn span so App Insights shows true mouth-to-ear latency + barge-in.
  function mark(name) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "client." + name, t: Date.now() }));
    }
  }

  function setStatus(text, cls) { statusEl.textContent = text; dot.className = "dot " + cls; }

  function addMsg(kind, who, text) {
    const el = document.createElement("div");
    el.className = "msg " + kind;
    if (who) { const w = document.createElement("div"); w.className = "who"; w.textContent = who; el.appendChild(w); }
    const t = document.createElement("span"); t.textContent = text; el.appendChild(t);
    logEl.appendChild(el); logEl.scrollTop = logEl.scrollHeight;
    return t;
  }
  const sys = (t) => addMsg("sys", null, t);

  // ---- playback (schedule PCM16 chunks back-to-back) ----
  function initPlayback() {
    playCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: SAMPLE_RATE });
    playHead = playCtx.currentTime;
  }
  function playChunk(int16) {
    if (!playCtx) return;
    const f32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) f32[i] = int16[i] / 0x8000;
    const buf = playCtx.createBuffer(1, f32.length, SAMPLE_RATE);
    buf.copyToChannel(f32, 0);
    const src = playCtx.createBufferSource();
    src.buffer = buf; src.connect(playCtx.destination);
    const now = playCtx.currentTime;
    if (playHead < now) playHead = now;
    src.start(playHead);
    playHead += buf.duration;
    activeSources.push(src);
    src.onended = () => { activeSources = activeSources.filter((s) => s !== src); };
  }
  function stopPlayback() { // barge-in: kill queued audio
    activeSources.forEach((s) => { try { s.stop(); } catch {} });
    activeSources = [];
    if (playCtx) playHead = playCtx.currentTime;
  }

  function b64ToInt16(b64) {
    const bin = atob(b64); const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new Int16Array(bytes.buffer);
  }
  function int16ToB64(int16) {
    const bytes = new Uint8Array(int16.buffer);
    let bin = ""; for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }

  // ---- mic capture ----
  async function startMic() {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: SAMPLE_RATE });
    await audioCtx.audioWorklet.addModule("pcm-worklet.js");
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    });
    source = audioCtx.createMediaStreamSource(micStream);
    workletNode = new AudioWorkletNode(audioCtx, "pcm-capture");
    workletNode.port.onmessage = (e) => {
      const pcm = e.data; // Int16Array
      // simple level meter
      let peak = 0; for (let i = 0; i < pcm.length; i += 32) peak = Math.max(peak, Math.abs(pcm[i]));
      micLevel.style.width = Math.min(100, (peak / 0x7fff) * 140) + "%";
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "input_audio_buffer.append", audio: int16ToB64(pcm) }));
      }
    };
    source.connect(workletNode);
    // Worklet needs a sink to pull audio in some browsers; route to a muted gain.
    const mute = audioCtx.createGain(); mute.gain.value = 0;
    workletNode.connect(mute); mute.connect(audioCtx.destination);
  }

  function stopMic() {
    if (workletNode) { workletNode.port.onmessage = null; try { workletNode.disconnect(); } catch {} }
    if (source) try { source.disconnect(); } catch {}
    if (micStream) micStream.getTracks().forEach((t) => t.stop());
    if (audioCtx) try { audioCtx.close(); } catch {}
    workletNode = source = micStream = audioCtx = null;
    micLevel.style.width = "0%";
  }

  // ---- websocket ----
  function connect() {
    connectBtn.disabled = true;
    setStatus("Connecting…", "connecting");
    ws = new WebSocket(backendWsUrl);

    ws.onopen = async () => {
      setStatus("Connected · starting mic…", "connecting");
      // Configure the realtime session. Do NOT send instructions/voice for a
      // Configure the realtime session. For a custom agent, keep this MINIMAL — the
      // agent's own metadata drives voice, VAD and transcription. Overriding
      // input_audio_transcription.model (e.g. whisper-1) is rejected; audio defaults
      // are already PCM16 @ 24 kHz, matching our capture.
      ws.send(JSON.stringify({
        type: "session.update",
        session: { modalities: ["text", "audio"] }
      }));
      try {
        initPlayback();
        await startMic();
        setStatus("Live · speak now", "live");
        stopBtn.disabled = false;
        sys("Connected. Start talking — the agent replies with voice.");
      } catch (err) {
        sys("Mic error: " + err.message); disconnect();
      }
    };

    ws.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      switch (m.type) {
        case "session.created":
        case "session.updated":
          break;
        case "input_audio_buffer.speech_started":
          stopPlayback(); // barge-in
          if (turnAudioStarted) mark("barge_in"); // user cut in over agent audio
          curAgentMsg = null;
          turnAudioStarted = false;
          break;
        case "input_audio_buffer.speech_stopped":
          mark("speech_stopped"); // user finished — clock starts for mouth-to-ear
          break;
        case "conversation.item.input_audio_transcription.completed":
          if (m.transcript) addMsg("user", "You", m.transcript.trim());
          break;
        case "response.audio.delta":
          if (m.delta) {
            if (!turnAudioStarted) { turnAudioStarted = true; mark("first_audio_played"); }
            playChunk(b64ToInt16(m.delta));
          }
          break;
        case "response.audio_transcript.delta":
        case "response.text.delta":
          if (!curAgentMsg) curAgentMsg = addMsg("agent", "Agent", "");
          curAgentMsg.textContent += m.delta || "";
          logEl.scrollTop = logEl.scrollHeight;
          break;
        case "response.done":
          curAgentMsg = null;
          turnAudioStarted = false;
          break;
        case "error":
          sys("Server error: " + JSON.stringify(m.error || m));
          break;
      }
    };

    ws.onerror = () => setStatus("Connection error", "error");
    ws.onclose = (e) => {
      setStatus("Disconnected" + (e.code && e.code !== 1000 ? ` (code ${e.code})` : ""), "idle");
      cleanup();
    };
  }

  function disconnect() { if (ws) try { ws.close(1000); } catch {}; cleanup(); }
  function cleanup() {
    stopMic(); stopPlayback();
    if (playCtx) { try { playCtx.close(); } catch {} playCtx = null; }
    ws = null;
    connectBtn.disabled = false; stopBtn.disabled = true;
  }

  connectBtn.onclick = connect;
  stopBtn.onclick = () => { sys("Ended."); disconnect(); setStatus("Disconnected", "idle"); };
})();
