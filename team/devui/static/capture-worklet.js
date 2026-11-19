// AudioWorklet: microphone -> mono -> 16 kHz -> Int16 LE -> exact 640-byte (20 ms) frames.
// `sampleRate` is the AudioContext rate (device dependent: 44100, 48000, ...), read at runtime.
import { downmix, StreamResampler, floatToInt16, FramePacker, rmsInt16LE, TARGET_RATE } from './pcm.js';

class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.rs = new StreamResampler(sampleRate, TARGET_RATE);
    this.packer = new FramePacker();
    this.channels = -1;
    this.clipIn = 0;
    this.clipOut = 0;
    this.stopped = false;
    this.port.onmessage = (e) => { if (e.data === 'stop') this.stopped = true; };
    this.port.postMessage({ kind: 'info', contextRate: sampleRate });
  }

  process(inputs) {
    if (this.stopped) return false;
    const input = inputs[0];
    if (!input || input.length === 0) return true;      // not connected yet / no channels
    if (input.length !== this.channels) {
      this.channels = input.length;
      this.port.postMessage({ kind: 'channels', channels: this.channels });
    }
    const mono = downmix(input);
    for (let i = 0; i < mono.length; i++) if (mono[i] >= 1 || mono[i] <= -1) this.clipIn++;
    const { int16, clipped } = floatToInt16(this.rs.process(mono));
    this.clipOut += clipped;
    for (const frame of this.packer.push(int16)) {
      this.port.postMessage({ kind: 'frame', frame, rms: rmsInt16LE(frame), clipIn: this.clipIn, clipOut: this.clipOut },
                            [frame]);
      this.clipIn = 0;
      this.clipOut = 0;
    }
    return true;
  }
}

registerProcessor('pcm-capture', PcmCapture);
