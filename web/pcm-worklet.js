// AudioWorklet processor: captures mono Float32 mic frames and posts them as
// 16-bit PCM (Int16) to the main thread. The AudioContext runs at 24 kHz so no
// resampling is needed for the Voice Live API (expects PCM16 @ 24 kHz).
//
// `process` is called every 128 samples (~5.3 ms @ 24 kHz). Posting one WebSocket
// message per call is ~188 tiny JSON+base64 sends/sec, which burns CPU and creates
// GC jitter on both the browser and the Python proxy. We instead ACCUMULATE samples
// into ~`batchSamples` chunks (default ~20 ms) before posting — ~4x fewer messages
// for a few ms of buffering that is imperceptible next to the model's response time.
class PCMCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const bs = options && options.processorOptions && options.processorOptions.batchSamples;
    this.batchSamples = Math.max(128, bs || 480); // 480 = 20 ms @ 24 kHz
    this.buf = new Int16Array(this.batchSamples);
    this.n = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (input && input[0]) {
      const chan = input[0];
      for (let i = 0; i < chan.length; i++) {
        let s = Math.max(-1, Math.min(1, chan[i]));
        this.buf[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
        if (this.n === this.batchSamples) {
          this.port.postMessage(this.buf, [this.buf.buffer]);
          this.buf = new Int16Array(this.batchSamples);
          this.n = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor("pcm-capture", PCMCaptureProcessor);
