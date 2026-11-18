"""Frame-level VAD and utterance accumulation.

The Endpointer only tracks *what was heard*: whether an utterance is open, how long the
current trailing silence is, and a version number that changes whenever speech resumes
after a pause. The *decision* to commit (end of turn) is made by the session, which can
combine silence length with the speculative STT transcript (see session.py).

Why not the example server's rule (RMS >= 500, 800 ms silence)?
  * 800 ms fixed silence cuts customers who pause mid-sentence (reading an order number,
    thinking) -> the agent talks over them, which the evaluator counts as an interruption.
  * A fixed RMS threshold breaks on quiet callers / line noise (see team/docs/REPORT.md for
    the demo-audio measurements).
"""
from __future__ import annotations

from collections import deque

from .audio import FRAME_MS, RATE, rms

try:
    import webrtcvad  # webrtcvad-wheels
except Exception:  # pragma: no cover - optional dependency
    webrtcvad = None


class Endpointer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.vad = None
        if cfg.vad == "webrtc" and webrtcvad is not None:
            self.vad = webrtcvad.Vad(int(cfg.webrtc_aggressiveness))
        self.noise_floor = 0.0
        self.reset()

    # ------------------------------------------------------------------
    def reset(self):
        self.active = False
        self.preroll = deque(maxlen=max(1, self.cfg.preroll_ms // FRAME_MS))
        self.onset_window = deque(maxlen=max(2, 2 * self.cfg.min_speech_ms // FRAME_MS))
        self.utt = bytearray()
        self.silence_ms = 0
        self.speech_ms = 0
        self.utt_ms = 0
        self.version = 0
        self.last_speech_end_bytes = 0

    def is_speech(self, frame: bytes) -> bool:
        r = rms(frame)
        gate = max(self.cfg.energy_min_rms, min(self.noise_floor * self.cfg.noise_floor_ratio, 3000.0))
        sp = r >= gate
        if sp and self.vad is not None:
            try:
                sp = self.vad.is_speech(frame, RATE)
            except Exception:
                pass  # malformed frame size -> fall back to energy decision
        if not sp:
            self.noise_floor = r if self.noise_floor == 0 else 0.95 * self.noise_floor + 0.05 * r
        return sp

    def feed(self, frame: bytes) -> str | None:
        """Returns 'start' when an utterance opens, 'resume' when speech comes back after
        a pause long enough to have triggered speculative STT, else None."""
        sp = self.is_speech(frame)
        if not self.active:
            self.preroll.append(frame)
            self.onset_window.append(sp)
            if sum(self.onset_window) * FRAME_MS >= self.cfg.min_speech_ms:
                self.active = True
                self.utt = bytearray(b"".join(self.preroll))
                self.utt_ms = len(self.preroll) * FRAME_MS
                self.speech_ms = sum(self.onset_window) * FRAME_MS
                self.silence_ms = 0
                self.last_speech_end_bytes = len(self.utt)
                return "start"
            return None

        self.utt.extend(frame)
        self.utt_ms += FRAME_MS
        if sp:
            ev = "resume" if self.silence_ms >= self.cfg.stt_trigger_silence_ms else None
            if ev:
                self.version += 1
            self.silence_ms = 0
            self.speech_ms += FRAME_MS
            self.last_speech_end_bytes = len(self.utt)
            return ev
        self.silence_ms += FRAME_MS
        return None

    def utterance_audio(self, tail_ms: int = 300) -> bytes:
        """Utterance audio up to the last voiced frame plus a short tail (long trailing
        silence makes Whisper-style models hallucinate)."""
        end = min(len(self.utt), self.last_speech_end_bytes + RATE * 2 * tail_ms // 1000)
        return bytes(self.utt[:end])
