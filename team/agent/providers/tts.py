"""Text-to-speech providers. Output is always 16 kHz s16le mono PCM.

SapiTTS uses the Windows SAPI5 voice installed on this machine (local, no network).
ToneTTS is a test double (a beep whose length follows the text) - NOT speech.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from ..audio import RATE

log = logging.getLogger("agent.tts")

SAFT16kHz16BitMono = 18
SVSFIsNotXML = 16  # never interpret LLM output as SAPI XML


class SapiTTS:
    name = "sapi"

    def __init__(self, cfg):
        self.cfg = cfg
        self.local = threading.local()
        self.pool = ThreadPoolExecutor(max_workers=cfg.tts_workers, thread_name_prefix="tts",
                                       initializer=self._init_thread)
        # fail fast at startup if the voice is missing
        self.voice_name = self.pool.submit(lambda: self.local.voice_name).result()
        log.info("TTS SAPI voice=%s rate=%s", self.voice_name, cfg.tts_rate)

    def _init_thread(self):
        import comtypes
        import comtypes.client as cc
        comtypes.CoInitialize()
        v = cc.CreateObject("SAPI.SpVoice")
        toks = v.GetVoices()
        chosen = None
        for i in range(toks.Count):
            if self.cfg.sapi_voice.lower() in toks.Item(i).GetDescription().lower():
                chosen = toks.Item(i)
                break
        if chosen is None:
            raise RuntimeError(f"SAPI voice containing {self.cfg.sapi_voice!r} not installed")
        v.Voice = chosen
        v.Rate = int(self.cfg.tts_rate)
        self.local.cc = cc
        self.local.voice = v
        self.local.voice_name = chosen.GetDescription()

    def _run(self, text: str) -> bytes:
        cc, v = self.local.cc, self.local.voice
        ms = cc.CreateObject("SAPI.SpMemoryStream")
        fmt = cc.CreateObject("SAPI.SpAudioFormat")
        fmt.Type = SAFT16kHz16BitMono
        ms.Format = fmt
        v.AudioOutputStream = ms
        v.Speak(text, SVSFIsNotXML)
        return bytes(ms.GetData())

    async def synth(self, text: str) -> bytes:
        return await asyncio.get_running_loop().run_in_executor(self.pool, self._run, text)


class OpenAITTS:
    """OpenAI /audio/speech (default model gpt-4o-mini-tts), streamed.

    response_format=pcm is 24 kHz s16le mono; it is resampled to 16 kHz chunk by chunk so the
    first audio can be forwarded before synthesis finishes (measured first chunk ~0.9 s)."""
    name = "openai"
    SRC_RATE = 24000

    def __init__(self, cfg):
        from openai import AsyncOpenAI
        key = os.environ.get(cfg.audio_api_key_env or "", "")
        if not key:
            raise RuntimeError(f"env var {cfg.audio_api_key_env!r} is not set (TTS provider openai)")
        self.cfg = cfg
        self.client = AsyncOpenAI(api_key=key, base_url=cfg.audio_base_url, timeout=cfg.tts_timeout_s, max_retries=0)

    async def stream(self, text: str):
        from ..audio import StreamResampler
        rs = StreamResampler(self.SRC_RATE, RATE)
        kw = {"instructions": self.cfg.tts_instructions} if self.cfg.tts_instructions else {}
        async with self.client.audio.speech.with_streaming_response.create(
                model=self.cfg.tts_model, voice=self.cfg.tts_voice, input=text, response_format="pcm", **kw) as resp:
            async for chunk in resp.iter_bytes(4800):  # 100 ms at 24 kHz
                out = rs.process(chunk)
                if out:
                    yield out
        tail = rs.flush()
        if tail:
            yield tail

    async def synth(self, text: str) -> bytes:
        return b"".join([c async for c in self.stream(text)])


class ToneTTS:
    """TEST DOUBLE: 440 Hz tone, 0.2 s + 40 ms per character (max 3 s)."""
    name = "tone"

    def __init__(self, cfg):
        self.cfg = cfg

    async def synth(self, text: str) -> bytes:
        await asyncio.sleep(0.02)
        sec = min(3.0, 0.2 + 0.04 * len(text))
        t = np.arange(int(RATE * sec)) / RATE
        return (np.sin(2 * np.pi * 440 * t) * 6000).astype(np.int16).tobytes()
