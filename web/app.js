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
  // Backend WS base (same-origin host). The path (/realtime vs /realtime-model) is chosen
  // at connect time from the selected mode.
  const wsBase = cfg.backendWsBase ||
    `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
  const httpBase = `${location.protocol}//${location.host}`;

  function selectedMode() {
    const el = document.querySelector('input[name="mode"]:checked');
    return el ? el.value : "agent";
  }
  function routeForMode(mode) {
    return mode === "model" ? "/realtime-model" : "/realtime";
  }
  function setModeEnabled(enabled) {
    document.querySelectorAll('input[name="mode"]').forEach((r) => { r.disabled = !enabled; });
  }
  // Ask the backend which modes are available; disable the model toggle if not configured.
  fetch(httpBase + "/modes").then((r) => r.json()).then((m) => {
    if (!(m.model && m.model.available)) {
      const opt = $("modelOpt");
      const radio = opt && opt.querySelector("input");
      if (radio) radio.disabled = true;
      if (opt) { opt.classList.add("disabled"); opt.title = "Set GATEWAY_WS_URL_MODEL + APIM_SUBSCRIPTION_KEY_MODEL in .env to enable"; }
    }
  }).catch(() => {});

  let audioCtx = null, workletNode = null, micStream = null, source = null;
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
    // Batch ~20 ms of audio per WS message (override via VOICE_AGENT_CONFIG.micBatchMs).
    // 0 or unset -> 20 ms. Fewer, larger frames = less JSON/base64 CPU and GC jitter.
    const micBatchMs = Number(cfg.micBatchMs) || 20;
    const batchSamples = Math.max(128, Math.round((SAMPLE_RATE * micBatchMs) / 1000));
    workletNode = new AudioWorkletNode(audioCtx, "pcm-capture", { processorOptions: { batchSamples } });
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
  // The gateway/Foundry handshake costs ~1.3 s (a one-time, per-session cost — see
  // docs/benchmarks.md). To hide it, we *pre-warm* the socket the moment the user
  // shows intent (hover/focus/press on Connect) and start the mic in PARALLEL with
  // the socket on the actual click. getUserMedia needs a user gesture so it stays on
  // the click; the WebSocket does not, so it can open early.
  let ws = null;        // current socket (prewarmed or live)
  let wsMode = null;    // the mode the current socket was opened for
  let wsReady = null;   // Promise that resolves when the current socket is OPEN
  let isLive = false;   // true once the mic is streaming

  function handleServerMessage(ev) {
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
  }

  function attachSocketHandlers(socket) {
    socket.onmessage = handleServerMessage;
    socket.onerror = () => { if (socket === ws) setStatus("Connection error", "error"); };
    socket.onclose = (e) => {
      if (socket !== ws) return; // a stale/replaced socket closing — ignore
      const wasLive = isLive;
      ws = null; wsReady = null; wsMode = null; isLive = false;
      if (wasLive) {
        setStatus("Disconnected" + (e.code && e.code !== 1000 ? ` (code ${e.code})` : ""), "idle");
        cleanup();
      } else {
        // A prewarmed socket dropped (e.g. idle timeout) before going live: reset
        // quietly; the next hover/click will re-warm.
        setStatus("Idle", "idle");
        connectBtn.disabled = false;
      }
    };
  }

  // Open (or reuse) a socket for `mode` WITHOUT starting the mic. Returns a Promise
  // that resolves to the open socket. Reuses an existing prewarmed socket for the
  // same mode; discards a stale socket opened for a different mode.
  function openSocket(mode) {
    if (ws && wsMode === mode &&
        (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return wsReady;
    }
    if (ws) { try { ws.close(1000); } catch {} }
    const url = wsBase + routeForMode(mode);
    const socket = new WebSocket(url);
    ws = socket; wsMode = mode;
    wsReady = new Promise((resolve, reject) => {
      socket.addEventListener("open", () => {
        // Configure the realtime session up-front (safe before the mic exists).
        // For a custom agent keep this MINIMAL — the agent's own metadata drives
        // voice, VAD and transcription; audio defaults are already PCM16 @ 24 kHz.
        try { socket.send(JSON.stringify({ type: "session.update", session: { modalities: ["text", "audio"] } })); } catch {}
        resolve(socket);
      }, { once: true });
      socket.addEventListener("error", () => reject(new Error("socket error")), { once: true });
    });
    attachSocketHandlers(socket);
    return wsReady;
  }

  // Pre-warm on intent: opens the socket ahead of the click so the ~1.3 s handshake
  // overlaps the user reaching for the button. Idempotent and cheap.
  function prewarm() {
    if (isLive) return;
    const mode = selectedMode();
    if (ws && wsMode === mode &&
        (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    setStatus("Preparing…", "connecting");
    $("gwPill").textContent = `mode: ${mode} · ${wsBase + routeForMode(mode)}`;
    openSocket(mode)
      .then(() => { if (!isLive) setStatus("Ready — click Connect", "connecting"); })
      .catch(() => { /* swallow; the click will retry */ });
  }

  // Go live: reuse the prewarmed socket (or open now) AND start the mic in parallel.
  async function goLive() {
    if (isLive) return;
    connectBtn.disabled = true;
    setModeEnabled(false);
    isLive = true;
    const mode = selectedMode();
    $("gwPill").textContent = `mode: ${mode} · ${wsBase + routeForMode(mode)}`;
    setStatus("Connecting…", "connecting");
    try {
      initPlayback();
      // Socket may already be OPEN from prewarm(); mic must start on this gesture.
      const socketP = openSocket(mode);
      const micP = startMic();
      await Promise.all([socketP, micP]);
      setStatus("Live · speak now", "live");
      stopBtn.disabled = false;
      sys("Connected. Start talking — the agent replies with voice.");
    } catch (err) {
      isLive = false;
      sys("Connection/mic error: " + (err && err.message ? err.message : String(err)));
      disconnect();
    }
  }

  function disconnect() { if (ws) try { ws.close(1000); } catch {}; cleanup(); }
  function cleanup() {
    stopMic(); stopPlayback();
    if (playCtx) { try { playCtx.close(); } catch {} playCtx = null; }
    ws = null; wsReady = null; wsMode = null; isLive = false;
    connectBtn.disabled = false; stopBtn.disabled = true;
    setModeEnabled(true);
  }

  connectBtn.onclick = goLive;
  stopBtn.onclick = () => { sys("Ended."); disconnect(); setStatus("Disconnected", "idle"); };

  // Pre-warm triggers: the earliest signals of intent. Hover/focus/press all open
  // the socket ahead of the click so session startup is hidden from the user.
  ["pointerenter", "focus", "pointerdown"].forEach((evt) => connectBtn.addEventListener(evt, prewarm));
  // If the user switches mode before going live, the prewarmed socket is for the
  // wrong route — discard it and warm the new one.
  document.querySelectorAll('input[name="mode"]').forEach((r) => {
    r.addEventListener("change", () => {
      if (isLive) return;
      if (ws && wsMode !== selectedMode()) { try { ws.close(1000); } catch {} ws = null; wsReady = null; wsMode = null; }
      prewarm();
    });
  });
})();
