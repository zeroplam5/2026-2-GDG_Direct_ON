"""PCM helpers (16-bit signed little-endian mono)."""
from __future__ import annotations

import numpy as np

RATE = 16000
BYTES_PER_SAMPLE = 2
FRAME_MS = 20
FRAME_BYTES = RATE * BYTES_PER_SAMPLE * FRAME_MS // 1000  # 640


def rms(pcm: bytes) -> float:
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if len(a) else 0.0


def pcm_ms(pcm_len_bytes: int, rate: int = RATE) -> float:
    return pcm_len_bytes * 1000.0 / (rate * BYTES_PER_SAMPLE)


def pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate or not pcm:
        return pcm
    from math import gcd
    from scipy.signal import resample_poly
    g = gcd(src_rate, dst_rate)
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    y = resample_poly(a, dst_rate // g, src_rate // g)
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


class StreamResampler:
    """Chunk-by-chunk polyphase resampler (e.g. 24 kHz TTS stream -> 16 kHz) with filter state
    carried across chunks, so streamed audio has no clicks at chunk boundaries."""

    def __init__(self, src_rate: int, dst_rate: int, taps_per_phase: int = 24):
        from math import gcd
        from scipy.signal import firwin
        g = gcd(src_rate, dst_rate)
        self.up, self.down = dst_rate // g, src_rate // g
        m = max(self.up, self.down)
        self.taps = (firwin(taps_per_phase * m + 1, 1.0 / m) * self.up).astype(np.float64)
        self.zi = np.zeros(len(self.taps) - 1)
        self.pos = 0          # global sample index (upsampled domain) of the next block start
        self.carry = b""

    def process(self, data: bytes) -> bytes:
        from scipy.signal import lfilter
        data = self.carry + data
        n = len(data) // 2 * 2
        self.carry = data[n:]
        if n == 0:
            return b""
        x = np.frombuffer(data[:n], dtype=np.int16).astype(np.float64)
        u = np.zeros(len(x) * self.up)
        u[::self.up] = x
        y, self.zi = lfilter(self.taps, 1.0, u, zi=self.zi)
        y = y[(-self.pos) % self.down::self.down]
        self.pos += len(u)
        return np.clip(np.round(y), -32768, 32767).astype(np.int16).tobytes()

    def flush(self) -> bytes:
        tail = (len(self.taps) // 2) // self.up + 1     # push out the filter delay
        out = self.process(b"\0\0" * tail)
        return out


def trim_leading_silence(pcm: bytes, threshold: float = 200.0, keep_ms: int = 20) -> bytes:
    """Drop near-silent audio at the start of a TTS clip (reduces time-to-first-sound).
    Returns b"" if the whole chunk is silent (streaming: wait for the next chunk)."""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    a = np.frombuffer(pcm, dtype=np.int16)
    if not len(a):
        return b""
    win = RATE * FRAME_MS // 1000 // 2  # 10 ms windows
    n = len(a) // win
    if n == 0:
        return b""
    e = np.sqrt((a[: n * win].astype(np.float32).reshape(n, win) ** 2).mean(1))
    if not (e >= threshold).any():
        return b""
    idx = int(np.argmax(e >= threshold))
    start = max(0, idx * win - RATE * keep_ms // 1000)
    return a[start:].tobytes()


class FrameAssembler:
    """Re-chunks arbitrary binary messages into exact 20 ms frames.

    The starter kit sends 640-byte frames, but an odd byte count or a different chunk
    size must not crash the session (audio-format robustness)."""

    def __init__(self, frame_bytes: int = FRAME_BYTES):
        self.frame_bytes = frame_bytes
        self.buf = bytearray()

    def push(self, data: bytes):
        self.buf.extend(data)
        while len(self.buf) >= self.frame_bytes:
            frame = bytes(self.buf[: self.frame_bytes])
            del self.buf[: self.frame_bytes]
            yield frame


def chunk(pcm: bytes, size: int):
    for i in range(0, len(pcm), size):
        c = pcm[i:i + size]
        if len(c) % 2:
            c = c[:-1]
        if c:
            yield c
