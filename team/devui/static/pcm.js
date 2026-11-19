// PCM helpers shared by the capture AudioWorklet, the page and the Node tests.
// Wire format (starter kit): 16 kHz, signed 16-bit little-endian, mono, 20 ms = 320 samples = 640 bytes.

export const TARGET_RATE = 16000;
export const FRAME_SAMPLES = 320;
export const FRAME_BYTES = FRAME_SAMPLES * 2;

/** Average all channels into one (browsers may deliver 1, 2 or more channels). */
export function downmix(channels) {
  if (!channels || channels.length === 0) return new Float32Array(0);
  if (channels.length === 1) return channels[0];
  const n = channels[0].length;
  const out = new Float32Array(n);
  for (const ch of channels) for (let i = 0; i < n; i++) out[i] += ch[i];
  const k = 1 / channels.length;
  for (let i = 0; i < n; i++) out[i] *= k;
  return out;
}

/**
 * Streaming windowed-sinc resampler (any ratio, e.g. 48000/44100 -> 16000 or 16000 -> 48000).
 * Filter state is carried across calls, so chunk boundaries do not click. For downsampling the
 * cut-off is 0.95 * the target Nyquist (anti-aliasing). Delay: halfTaps / cutoff source samples.
 */
export class StreamResampler {
  constructor(srcRate, dstRate = TARGET_RATE, halfTaps = 16) {
    this.src = srcRate;
    this.dst = dstRate;
    this.step = srcRate / dstRate;                       // source samples per output sample
    this.fc = Math.min(1, dstRate / srcRate) * 0.95;      // cut-off relative to source Nyquist
    this.W = Math.ceil(halfTaps / this.fc);               // half-width in source samples
    this.buf = new Float32Array(this.W);                  // zero history so output 0 = source 0
    this.pos = this.W;                                    // fractional source index of next output
    this.bypass = srcRate === dstRate;
    // kernel table: RES points per source sample over [-W, W], linear interpolation in between
    this.RES = 256;
    const n = 2 * this.W * this.RES + 2;
    this.table = new Float32Array(n);
    for (let k = 0; k < n; k++) this.table[k] = this._exact(k / this.RES - this.W);
  }

  _exact(x) {
    // fc * sinc(fc * x) * Blackman window over [-W, W]
    if (Math.abs(x) >= this.W) return 0;
    const a = Math.PI * this.fc * x;
    const s = x === 0 ? 1 : Math.sin(a) / a;
    const w = 0.42 + 0.5 * Math.cos(Math.PI * x / this.W) + 0.08 * Math.cos(2 * Math.PI * x / this.W);
    return this.fc * s * w;
  }

  _kernel(x) {
    const p = (x + this.W) * this.RES;
    if (p <= 0 || p >= this.table.length - 1) return 0;
    const i = Math.floor(p);
    const f = p - i;
    return this.table[i] + (this.table[i + 1] - this.table[i]) * f;
  }

  process(input) {
    if (this.bypass) return Float32Array.from(input);
    const buf = new Float32Array(this.buf.length + input.length);
    buf.set(this.buf, 0);
    buf.set(input, this.buf.length);
    const out = [];
    const W = this.W;
    let pos = this.pos;
    while (Math.floor(pos) + W < buf.length) {
      const c = Math.floor(pos);
      let acc = 0, norm = 0;
      for (let i = c - W + 1; i <= c + W; i++) {
        const h = this._kernel(pos - i);
        acc += buf[i] * h;
        norm += h;
      }
      out.push(norm > 0 ? acc / norm : 0);              // exact unity gain at DC
      pos += this.step;
    }
    const keepFrom = Math.max(0, Math.floor(pos) - W + 1);
    this.buf = buf.slice(keepFrom);
    this.pos = pos - keepFrom;
    return Float32Array.from(out);
  }

  /** Push zeros through the filter so the last real samples come out (end of a stream). */
  flush() {
    return this.bypass ? new Float32Array(0) : this.process(new Float32Array(this.W + 1));
  }
}

/** Float [-1, 1] -> Int16 with clipping. Returns { int16, clipped } (clipped = samples beyond full scale). */
export function floatToInt16(f32) {
  const out = new Int16Array(f32.length);
  let clipped = 0;
  for (let i = 0; i < f32.length; i++) {
    let v = f32[i];
    if (v >= 1) { v = 1; clipped++; } else if (v <= -1) { v = -1; clipped++; } else if (Number.isNaN(v)) v = 0;
    out[i] = v < 0 ? Math.round(v * 32768) : Math.round(v * 32767);
  }
  return { int16: out, clipped };
}

/** Collects Int16 samples and emits exact 640-byte little-endian frames (ArrayBuffer). */
export class FramePacker {
  constructor(frameSamples = FRAME_SAMPLES) {
    this.n = frameSamples;
    this.cur = new Int16Array(frameSamples);
    this.fill = 0;
  }

  push(int16) {
    const frames = [];
    let i = 0;
    while (i < int16.length) {
      const take = Math.min(this.n - this.fill, int16.length - i);
      this.cur.set(int16.subarray(i, i + take), this.fill);
      this.fill += take;
      i += take;
      if (this.fill === this.n) {
        const ab = new ArrayBuffer(this.n * 2);
        const dv = new DataView(ab);
        for (let k = 0; k < this.n; k++) dv.setInt16(k * 2, this.cur[k], true); // explicit little-endian
        frames.push(ab);
        this.fill = 0;
      }
    }
    return frames;
  }
}

/** RMS of an Int16 frame in int16 units (same scale as the server's energy_min_rms / evaluator SILENCE_RMS). */
export function rmsInt16LE(ab) {
  const dv = new DataView(ab);
  const n = ab.byteLength >> 1;
  if (!n) return 0;
  let s = 0;
  for (let i = 0; i < n; i++) { const v = dv.getInt16(i * 2, true); s += v * v; }
  return Math.sqrt(s / n);
}

/** Decodes a stream of s16le byte chunks into Float32, carrying an odd trailing byte to the next chunk. */
export class S16Decoder {
  constructor() { this.carry = null; }

  push(ab) {
    let bytes = new Uint8Array(ab);
    if (this.carry !== null) {
      const b = new Uint8Array(bytes.length + 1);
      b[0] = this.carry; b.set(bytes, 1); bytes = b;
      this.carry = null;
    }
    if (bytes.length % 2) { this.carry = bytes[bytes.length - 1]; bytes = bytes.subarray(0, bytes.length - 1); }
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
    const out = new Float32Array(bytes.length >> 1);
    for (let i = 0; i < out.length; i++) out[i] = dv.getInt16(i * 2, true) / 32768;
    return out;
  }
}
