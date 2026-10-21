// The Playground's in-page tap for the Analyzer (an AudioWorklet; js/playground.js loads it).
//
// Two stereo inputs: 0 = what is heard as "before", 1 = what is heard as "after" (the same sound
// when nothing is being compared). Every ~1024 frames it posts
//   { before: Float32Array(2048), after: Float32Array(2048) }      (interleaved L R, buffers transferred)
// to the page, which hands them to VEXT.audioBus.pushLocal. It only collects while the page says
// `{ on: true }` (the Analyzer listens and something plays here); silent otherwise, so an idle page
// costs nothing but the empty process() call.
class AnalyzerTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = 1024;
    this.on = false;
    this.n = 0;
    this.before = new Float32Array(this.size * 2);
    this.after = new Float32Array(this.size * 2);
    this.port.onmessage = (e) => {
      if (!e.data || !('on' in e.data)) return;
      this.on = !!e.data.on;
      this.n = 0;   // a block never mixes old and new sound
    };
  }

  process(inputs) {
    if (!this.on) return true;
    const a = inputs[0] || [];
    const b = inputs[1] || [];
    const frames = (a[0] || b[0] || { length: 128 }).length;
    const aL = a[0], aR = a[1] || a[0];
    const bL = b[0], bR = b[1] || b[0];
    for (let i = 0; i < frames; i++) {
      const k = (this.n + i) * 2;
      this.before[k] = aL ? aL[i] : 0;
      this.before[k + 1] = aR ? aR[i] : 0;
      this.after[k] = bL ? bL[i] : 0;
      this.after[k + 1] = bR ? bR[i] : 0;
    }
    this.n += frames;
    if (this.n >= this.size) {
      const before = this.before;
      const after = this.after;
      this.before = new Float32Array(this.size * 2);
      this.after = new Float32Array(this.size * 2);
      this.n = 0;
      this.port.postMessage({ before, after }, [before.buffer, after.buffer]);
    }
    return true;
  }
}

registerProcessor('vext-analyzer-tap', AnalyzerTap);
