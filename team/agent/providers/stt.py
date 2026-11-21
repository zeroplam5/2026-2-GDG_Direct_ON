"""Speech-to-text providers.

FasterWhisperSTT runs locally (no audio leaves the machine). FakeSTT is a test double that
returns configured text without listening to the audio; it is NOT speech recognition.
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ..audio import pcm_to_float

log = logging.getLogger("agent.stt")

# Phrases Whisper is known to hallucinate on silence / noise (Korean YouTube artefacts).
_HALLUCINATIONS = ("시청해주셔서 감사합니다", "구독과 좋아요", "MBC 뉴스", "자막 제공", "감사합니다.",
                  "한글자막", "자막 by", "by 한효정")  # Whisper outputs these on silence / noise (subtitle credits)


@dataclass
class STTResult:
    text: str
    ms: float
    detail: dict


def _add_cuda_dll_dirs():
    """pip-installed nvidia-cublas/cudnn wheels put DLLs under site-packages/nvidia/*/bin."""
    if os.name != "nt":
        return
    for d in glob.glob(os.path.join(sys.prefix, "Lib", "site-packages", "nvidia", "*", "bin")):
        try:
            os.add_dll_directory(d)
        except OSError:
            pass
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


class FasterWhisperSTT:
    name = "faster_whisper"

    def __init__(self, cfg):
        _add_cuda_dll_dirs()
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from faster_whisper import WhisperModel
        import numpy as np

        self.cfg = cfg
        devices = ["cuda", "cpu"] if cfg.stt_device == "auto" else [cfg.stt_device]
        last = None
        for dev in devices:
            ct = cfg.stt_compute_type if cfg.stt_compute_type != "auto" else ("float16" if dev == "cuda" else "int8")
            try:
                self.model = WhisperModel(cfg.whisper_model, device=dev, compute_type=ct, num_workers=cfg.stt_workers)
                list(self.model.transcribe(np.zeros(16000, dtype=np.float32), language=cfg.stt_language)[0])  # warm-up / DLL check
                self.device, self.compute_type = dev, ct
                break
            except Exception as e:  # e.g. missing cuBLAS -> fall back to CPU
                last = e
                log.warning("faster-whisper on %s failed: %s", dev, repr(e)[:200])
        else:
            raise RuntimeError(f"faster-whisper could not start: {last!r}")
        self.pool = ThreadPoolExecutor(max_workers=cfg.stt_workers, thread_name_prefix="stt")
        log.info("STT faster-whisper model=%s device=%s compute=%s", cfg.whisper_model, self.device, self.compute_type)

    def _run(self, pcm: bytes) -> STTResult:
        t = time.perf_counter()
        segs, info = self.model.transcribe(
            pcm_to_float(pcm), language=self.cfg.stt_language or None, beam_size=self.cfg.stt_beam_size,
            condition_on_previous_text=False, without_timestamps=True, vad_filter=False,
        )
        kept, dropped = [], 0
        for s in segs:
            txt = s.text.strip()
            if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:
                dropped += 1
                continue
            if any(h in txt for h in _HALLUCINATIONS) and len(txt) < 25:
                dropped += 1
                continue
            kept.append(txt)
        return STTResult(" ".join(kept).strip(), (time.perf_counter() - t) * 1000,
                         {"language": info.language, "dropped_segments": dropped})

    async def transcribe(self, pcm: bytes, ctx: dict | None = None) -> STTResult:
        return await asyncio.get_running_loop().run_in_executor(self.pool, self._run, pcm)


def _wav_bytes(pcm: bytes, rate: int = 16000) -> bytes:
    import io
    import wave
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return b.getvalue()


class OpenAISTT:
    """OpenAI /audio/transcriptions (default model gpt-4o-mini-transcribe).

    The customer's audio is SENT TO OPENAI. Tail latency: measured 0.55-1.3 s typical with a
    9.4 s outlier, so a second identical request is fired after stt_hedge_ms and whichever
    returns first wins (the loser is cancelled)."""
    name = "openai"

    def __init__(self, cfg):
        from openai import AsyncOpenAI
        key = os.environ.get(cfg.audio_api_key_env or "", "")
        if not key:
            raise RuntimeError(f"env var {cfg.audio_api_key_env!r} is not set (STT provider openai)")
        self.cfg = cfg
        self.client = AsyncOpenAI(api_key=key, base_url=cfg.audio_base_url, timeout=cfg.stt_timeout_s, max_retries=0)

    async def _once(self, wav: bytes) -> str:
        kw = {"language": self.cfg.stt_language} if self.cfg.stt_language else {}
        if self.cfg.stt_prompt:
            kw["prompt"] = self.cfg.stt_prompt
        r = await self.client.audio.transcriptions.create(model=self.cfg.stt_model, file=("audio.wav", wav, "audio/wav"), **kw)
        return (r.text or "").strip()

    async def transcribe(self, pcm: bytes, ctx: dict | None = None) -> STTResult:
        t = time.perf_counter()
        wav = _wav_bytes(pcm)
        tasks = [asyncio.create_task(self._once(wav))]
        hedged = False
        try:
            while True:
                timeout = None
                if not hedged and self.cfg.stt_hedge_ms > 0:
                    timeout = max(0.0, self.cfg.stt_hedge_ms / 1000 - (time.perf_counter() - t))
                done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if not done:  # 1st request is slow -> hedge
                    hedged = True
                    tasks.append(asyncio.create_task(self._once(wav)))
                    continue
                ok = [d for d in done if d.exception() is None]
                if ok:
                    text = ok[0].result()
                    break
                tasks = [x for x in tasks if x not in done]
                if not tasks:
                    raise next(iter(done)).exception()
        finally:
            for x in tasks:
                if not x.done():
                    x.cancel()
        if any(h in text for h in _HALLUCINATIONS) and len(text) < 25:
            text = ""
        return STTResult(text, (time.perf_counter() - t) * 1000, {"hedged": hedged})


class SonioxSTT:
    """Soniox real-time STT over WebSocket (11/21 official STT). NOT yet tested with a real key.

    Fits the existing batch interface used by Session (speculative STT at stt_trigger_silence_ms):
    each call opens one WebSocket, sends the utterance audio as fast as possible, signals
    end-of-audio with an empty frame and collects the final tokens until "finished".
    The customer's audio is SENT TO SONIOX.
    Protocol: https://soniox.com/docs/api-reference/stt/websocket-api"""
    name = "soniox"

    def __init__(self, cfg):
        key = os.environ.get(cfg.soniox_api_key_env or "", "")
        if not key:
            raise RuntimeError(f"env var {cfg.soniox_api_key_env!r} is not set (STT provider soniox)")
        self.cfg = cfg
        self._key = key

    def _config_msg(self) -> dict:
        return {
            "api_key": self._key,
            "model": self.cfg.soniox_model,
            "audio_format": "pcm_s16le",
            "sample_rate": 16000,
            "num_channels": 1,
            "language_hints": [h.strip() for h in self.cfg.soniox_language_hints.split(",") if h.strip()],
        }

    async def _once(self, pcm: bytes) -> tuple[str, dict]:
        import websockets
        step = max(640, 32 * self.cfg.soniox_chunk_ms)  # bytes per chunk at 16 kHz s16le
        final: list[str] = []
        partial = ""
        # Bearer header: required by the baze gateway; the official Soniox URL ignores it (api_key below).
        async with websockets.connect(self.cfg.soniox_url, max_size=None, open_timeout=self.cfg.stt_timeout_s,
                                      additional_headers={"Authorization": f"Bearer {self._key}"}) as ws:
            await ws.send(json.dumps(self._config_msg()))
            for i in range(0, len(pcm), step):
                await ws.send(pcm[i:i + step])
            await ws.send("")  # end of audio -> server finalizes everything, then sends finished
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("error_code") or msg.get("error_message"):
                    raise RuntimeError(f"soniox error {msg.get('error_code')}: {msg.get('error_message')}")
                tmp = []
                for tok in msg.get("tokens", []):
                    t = tok.get("text", "")
                    if t in ("<end>", "<fin>"):
                        continue
                    (final.append(t) if tok.get("is_final") else tmp.append(t))
                partial = "".join(tmp)
                if msg.get("finished"):
                    partial = ""
                    break
        text = ("".join(final) + partial).strip()
        return text, {"unfinalized": bool(partial)}

    async def transcribe(self, pcm: bytes, ctx: dict | None = None) -> STTResult:
        t = time.perf_counter()
        text, detail = await asyncio.wait_for(self._once(pcm), timeout=self.cfg.stt_timeout_s)
        return STTResult(text, (time.perf_counter() - t) * 1000, detail)


class FakeSTT:
    """TEST DOUBLE. Returns AGENT_STT_FAKE_TEXT, or - if that is a path to a JSON file -
    the text mapped to session.start's case_id. Does not listen to the audio."""
    name = "fake"

    def __init__(self, cfg):
        self.text = cfg.stt_fake_text
        self.mapping = None
        if self.text.endswith(".json") and os.path.exists(self.text):
            with open(self.text, encoding="utf-8") as f:
                self.mapping = json.load(f)

    async def transcribe(self, pcm: bytes, ctx: dict | None = None) -> STTResult:
        await asyncio.sleep(0.05)
        if self.mapping is not None:
            text = self.mapping.get((ctx or {}).get("case_id", ""), "")
        else:
            text = self.text
        return STTResult(text, 50.0, {"fake": True})
