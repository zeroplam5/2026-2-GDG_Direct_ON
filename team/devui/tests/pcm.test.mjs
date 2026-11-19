// Node tests for the browser PCM conversion (run: node --test team/devui/tests/)
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  downmix, StreamResampler, floatToInt16, FramePacker, rmsInt16LE, S16Decoder, FRAME_BYTES, TARGET_RATE,
} from '../static/pcm.js';

const sine = (rate, hz, sec, amp = 0.5) => Float32Array.from({ length: Math.round(rate * sec) },
  (_, i) => amp * Math.sin(2 * Math.PI * hz * i / rate));

function chunked(rs, x, size = 128) {
  const parts = [];
  for (let i = 0; i < x.length; i += size) parts.push(rs.process(x.subarray(i, i + size)));
  const out = new Float32Array(parts.reduce((a, p) => a + p.length, 0));
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

// amplitude of a frequency component (Goertzel), on the steady-state middle part
function toneAmp(x, rate, hz) {
  const a = x.subarray(Math.floor(x.length * 0.2), Math.floor(x.length * 0.8));
  let re = 0, im = 0;
  for (let i = 0; i < a.length; i++) { const p = 2 * Math.PI * hz * i / rate; re += a[i] * Math.cos(p); im -= a[i] * Math.sin(p); }
  return 2 * Math.hypot(re, im) / a.length;
}

test('frames are exactly 640 bytes, little-endian', () => {
  const p = new FramePacker();
  const s = new Int16Array(1000);
  s[0] = 0x1234; s[1] = -2; s[320] = -32768; s[321] = 32767;
  const frames = p.push(s);
  assert.equal(frames.length, 3);                         // 960 samples -> 3 frames, 40 carried over
  for (const f of frames) assert.equal(f.byteLength, FRAME_BYTES);
  const b0 = new Uint8Array(frames[0]);
  assert.deepEqual([...b0.subarray(0, 4)], [0x34, 0x12, 0xfe, 0xff]);
  const b1 = new Uint8Array(frames[1]);
  assert.deepEqual([...b1.subarray(0, 4)], [0x00, 0x80, 0xff, 0x7f]);
  assert.equal(p.push(new Int16Array(280)).length, 1);    // 40 + 280 = 320 -> one more frame
});

test('float -> int16 clips and rounds', () => {
  const { int16, clipped } = floatToInt16(Float32Array.from([1.5, -1.2, 0.5, -0.5, 0, NaN, 1, -1]));
  assert.deepEqual([...int16], [32767, -32768, 16384, -16384, 0, 0, 32767, -32768]);
  assert.equal(clipped, 4);
});

test('downmix averages channels', () => {
  const m = downmix([Float32Array.from([1, 0.5]), Float32Array.from([0, -0.5])]);
  assert.deepEqual([...m], [0.5, 0]);
  const one = Float32Array.from([0.25]);
  assert.equal(downmix([one]), one);
});

for (const rate of [48000, 44100, 22050, 16000]) {
  test(`resample ${rate} -> 16000 keeps a 1 kHz tone and duration`, () => {
    const x = sine(rate, 1000, 1.0, 0.5);
    const y = chunked(new StreamResampler(rate, TARGET_RATE), x);
    assert.ok(Math.abs(y.length - 16000) <= 60, `length ${y.length}`);   // delay <= W source samples
    const a = toneAmp(y, 16000, 1000);
    assert.ok(Math.abs(a - 0.5) < 0.01, `amp ${a}`);
  });
}

test('resample 48000 -> 16000 removes content above 8 kHz (anti-aliasing)', () => {
  const y = chunked(new StreamResampler(48000, 16000), sine(48000, 10000, 1.0, 0.5));
  const mid = y.subarray(2000, 14000);
  const rms = Math.sqrt(mid.reduce((s, v) => s + v * v, 0) / mid.length);
  assert.ok(rms < 0.005, `alias rms ${rms}`);              // > 37 dB below the 0.35 rms input
});

test('streaming output does not depend on chunk size', () => {
  const x = sine(44100, 523, 0.5);
  const a = chunked(new StreamResampler(44100, 16000), x, 128);
  const b = chunked(new StreamResampler(44100, 16000), x, 1000);
  assert.equal(a.length, b.length);
  for (let i = 0; i < a.length; i++) assert.ok(Math.abs(a[i] - b[i]) < 1e-5);
});

test('upsampling for playback (16000 -> 48000) keeps the tone; flush emits the tail', () => {
  const rs = new StreamResampler(16000, 48000);
  const y1 = chunked(rs, sine(16000, 440, 1.0, 0.5), 320);
  const tail = rs.flush();
  assert.ok(Math.abs(y1.length + tail.length - 48000) <= 6, `len ${y1.length + tail.length}`);
  assert.ok(Math.abs(toneAmp(y1, 48000, 440) - 0.5) < 0.01);
});

test('full capture path: 48 kHz stereo -> 640-byte frames of matching duration', () => {
  const rs = new StreamResampler(48000, 16000);
  const p = new FramePacker();
  const l = sine(48000, 300, 2.0, 0.3), r = sine(48000, 300, 2.0, 0.3);
  let frames = 0, bytes = 0;
  for (let i = 0; i < l.length; i += 128) {
    const { int16 } = floatToInt16(rs.process(downmix([l.subarray(i, i + 128), r.subarray(i, i + 128)])));
    for (const f of p.push(int16)) { frames++; bytes += f.byteLength; assert.equal(f.byteLength, 640); }
  }
  assert.ok(Math.abs(frames - 100) <= 1, `frames ${frames}`); // 2 s = 100 x 20 ms
  assert.equal(bytes, frames * 640);
});

test('rms in int16 units and s16le decoding with odd chunk boundaries', () => {
  const { int16 } = floatToInt16(Float32Array.from({ length: 320 }, () => 0.1));
  const [f] = new FramePacker().push(int16);
  assert.ok(Math.abs(rmsInt16LE(f) - 3277) < 1);
  const d = new S16Decoder();
  const u = new Uint8Array(f);
  const a = d.push(u.slice(0, 3).buffer);                   // 1 sample + 1 carried byte
  const b = d.push(u.slice(3).buffer);
  assert.equal(a.length + b.length, 320);
  assert.ok(Math.abs(b[0] - 3277 / 32768) < 1e-4);
});
