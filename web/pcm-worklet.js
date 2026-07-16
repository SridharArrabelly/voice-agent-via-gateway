// AudioWorklet processor: captures mono Float32 mic frames and posts them as
// 16-bit PCM (Int16) to the main thread. The AudioContext runs at 24 kHz so no
// resampling is needed for the Voice Live API (expects PCM16 @ 24 kHz).
class PCMCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0]) {
      const chan = input[0];
      const pcm = new Int16Array(chan.length);
      for (let i = 0; i < chan.length; i++) {
        let s = Math.max(-1, Math.min(1, chan[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(pcm, [pcm.buffer]);
    }
    return true;
  }
}
registerProcessor("pcm-capture", PCMCaptureProcessor);
