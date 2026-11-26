// Runs on the audio thread: cuts the microphone into 40 ms frames of 16-bit PCM (960
// samples at the 24 kHz the AudioContext runs at) and posts each to voice.js.
const FRAME_SAMPLES = 960;

class MicFrames extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new Int16Array(FRAME_SAMPLES);
    this.filled = 0;
  }

  process(inputs) {
    const samples = inputs[0]?.[0];
    if (!samples) return true;
    for (const sample of samples) {
      const clipped = Math.max(-1, Math.min(1, sample));
      this.frame[this.filled++] = clipped < 0 ? clipped * 0x8000 : clipped * 0x7fff;
      if (this.filled === FRAME_SAMPLES) {
        const full = this.frame.buffer;
        this.port.postMessage(full, [full]);
        this.frame = new Int16Array(FRAME_SAMPLES);
        this.filled = 0;
      }
    }
    return true;
  }
}

registerProcessor("mic-frames", MicFrames);
