"""Opt-in streaming end-of-utterance with Soniox (AGENT_EOU_MODE=soniox_stream).

NOT tested with a real Soniox key (2026-11-20). Default mode is "vad" (local VAD + speculative
batch STT + sentence-completeness check in session.py), which this module does not touch.

Ported from the standalone experiment team/scripts/endpoint_server.py (feature/endpointing) into
the real agent, so endpointing can be compared with the full STT -> LLM -> functions -> TTS
pipeline and the starter-kit evaluator:
  * one Soniox WebSocket per session; every 20 ms frame from the caller is forwarded
  * final tokens are collected; the "<end>" token (Soniox endpoint detection) hands the text
    collected so far to the session as a pending end of utterance (kept until the session commits)
  * the session commits only when its own VAD also measured >= soniox_min_silence_ms of silence
    (same safety rule as the experiment server), and falls back to its local silence limit
    (soniox_fallback_silence_ms) + batch STT if Soniox never sends "<end>" or the stream fails
  * a new non-empty token after "<end>" means the customer resumed: the pending end is dropped

Also used by AGENT_STT_STREAM=1 (eou_mode=vad, stt_provider=soniox): the stream only transcribes
while the customer speaks, the local VAD still decides the end of the utterance ("<end>" is
ignored). At the commit the session sends {"type":"finalize"}, waits for "<fin>" (at most
stream_finalize_ms) and takes text() = final tokens + the last non-final tokens.

Authentication follows cfg.soniox_auth (providers/stt.soniox_auth): "config" (default, api_key in the
first message), "header" (Authorization: Bearer, as in the contest site example) or "both".
Which one Soniox accepts is not verified with a real key yet.
Endpoint parameter names follow the Soniox docs cited in team/docs/ENDPOINTING.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from .providers.stt import soniox_auth

log = logging.getLogger("agent.soniox_stream")


class SonioxStream:
    name = "soniox_stream"

    def __init__(self, cfg, on_token, on_end, sid: str = ""):
        key = os.environ.get(cfg.soniox_api_key_env or "", "")
        if not key:
            raise RuntimeError(f"env var {cfg.soniox_api_key_env!r} is not set (AGENT_EOU_MODE=soniox_stream)")
        self.cfg = cfg
        self._key = key
        self.on_token = on_token  # called for every non-empty spoken token (final or not)
        self.on_end = on_end      # called with the final text since the previous "<end>"
        self.sid = sid
        self.final: list[str] = []
        self.partial = ""        # non-final tokens of the latest message (Soniox resends them each time)
        self.fins = 0            # "<fin>" tokens received (answers to finalize())
        self._fin = asyncio.Event()
        self.sent_ms = 0         # audio queued so far, in stream time
        self.cut_ms = 0          # tokens starting before this (stream time) belong to a cleared turn
        self.q: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.error: str | None = None
        self.closed = False
        self.connected = False  # the session uses this stream only after the config was sent

    def config_msg(self) -> dict:
        c = self.cfg
        return {
            **soniox_auth(c, self._key)[1],
            "model": c.soniox_model,
            "audio_format": "pcm_s16le",
            "sample_rate": 16000,
            "num_channels": 1,
            "language_hints": [h.strip() for h in c.soniox_language_hints.split(",") if h.strip()],
            "enable_endpoint_detection": True,
            "max_endpoint_delay_ms": c.soniox_max_endpoint_delay_ms,
            "endpoint_latency_adjustment_level": c.soniox_endpoint_latency,
            "endpoint_sensitivity": c.soniox_endpoint_sensitivity,
        }

    @property
    def ok(self) -> bool:
        """True once connected and while healthy. Until then the session uses local VAD."""
        return self.connected and self.error is None and not self.closed

    def start(self):
        self.task = asyncio.create_task(self._run())

    def send(self, frame: bytes):
        # frames are queued while connecting too, so Soniox gets the whole utterance
        if self.error is None and not self.closed:
            self.q.put_nowait(frame)
            self.sent_ms += len(frame) * 1000 // 32000  # 16 kHz s16le mono

    def clear(self):
        """Forget the text so far. Tokens with a start time before now (late tokens of the cleared
        speech) are dropped when they arrive; tokens without timestamps are kept."""
        self.final.clear()
        self.partial = ""
        self.cut_ms = self.sent_ms

    def text(self) -> str:
        """Final tokens + the latest non-final ones since the last clear()."""
        return ("".join(self.final) + self.partial).strip()

    async def finalize(self, timeout_s: float) -> str:
        """Ask Soniox to finalize everything heard so far and wait for its "<fin>" token, at most
        timeout_s. Returns "fin", "timeout" (ignored / not supported, e.g. by a gateway) or
        "error" (stream failed or closed while waiting); the text so far is usable in every case."""
        if not self.ok:
            return "error"
        n = self.fins
        self.q.put_nowait(json.dumps({"type": "finalize"}))  # after the frames already queued
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout_s
        while self.fins == n:
            if not self.ok:
                return "error"
            left = end - loop.time()
            if left <= 0:
                return "timeout"
            self._fin.clear()
            try:
                await asyncio.wait_for(self._fin.wait(), min(left, 0.05))
            except TimeoutError:
                pass
        return "fin"

    async def _send_loop(self, ws):
        while True:
            f = await self.q.get()
            if f is None:
                await ws.send("")  # end of audio
                return
            await ws.send(f)  # bytes = audio, str = control message (finalize)

    async def _run(self):
        import websockets
        try:
            async with websockets.connect(self.cfg.soniox_url, max_size=None, open_timeout=self.cfg.stt_timeout_s,
                                          additional_headers=soniox_auth(self.cfg, self._key)[0]) as ws:
                await ws.send(json.dumps(self.config_msg()))
                self.connected = True
                sender = asyncio.create_task(self._send_loop(ws))
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("error_code") or msg.get("error_message"):
                            raise RuntimeError(f"soniox error {msg.get('error_code')}: {msg.get('error_message')}")
                        partial, fin = [], False
                        for tok in msg.get("tokens", []):
                            t = tok.get("text", "")
                            if t == "<end>":
                                # whole utterance so far: if the customer resumes after "<end>",
                                # the next "<end>" still carries the first part. The session
                                # clears the buffer when it commits and after its response.
                                self.on_end("".join(self.final).strip())
                            elif t == "<fin>":
                                fin = True
                            else:
                                if isinstance(tok.get("start_ms"), (int, float)) and tok["start_ms"] < self.cut_ms:
                                    continue  # late token of speech from before the last clear()
                                if t.strip():
                                    self.on_token(t)
                                (self.final if tok.get("is_final") else partial).append(t)
                        self.partial = "".join(partial)
                        if fin:
                            self.fins += 1
                            self._fin.set()
                        if msg.get("finished"):
                            break
                finally:
                    sender.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = f"{type(e).__name__}: {str(e)[:160]}"
            log.warning("[%s] soniox stream failed, falling back to local VAD + batch STT: %s", self.sid, self.error)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.q.put_nowait(None)
        if self.task and not self.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 1.0)
            except (asyncio.TimeoutError, Exception):
                self.task.cancel()
