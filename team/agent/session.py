"""One WebSocket connection = one Session. All per-call state lives here, so concurrent
sessions never share audio buffers, dialog history, tool clients or output.

Turn flow
  frames -> Endpointer -> (silence >= stt_trigger) speculative STT on the audio so far
         -> commit when (silence >= complete_silence AND transcript looks complete)
                     or (silence >= max_silence) or (utterance >= max_utterance)
         -> Dialog (LLM + tools) -> sentences -> TTS -> PCM frames -> agent.transcript
         -> {"type":"turn.end"}   (always sent, also on errors / budget timeout)

Speculative STT is safe: it has no side effects and is discarded if the customer keeps
talking. LLM / tool calls are NOT started speculatively because tool calls are recorded by
the function server (a premature call would be a wrong call).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from . import policy
from .audio import FRAME_BYTES, FRAME_MS, RATE, FrameAssembler, chunk, resample, trim_leading_silence
from .dialog import Ack, Dialog
from .endpointing import Endpointer
from .gate import Gate
from .textutil import looks_complete, normalize_for_tts
from .tools_client import ToolsClient

log = logging.getLogger("agent.session")


class ProtocolError(Exception):
    pass


class Session:
    def __init__(self, ws, app):
        self.ws = ws
        self.app = app
        self.cfg = app.cfg
        self.sid = "?"
        self.case_id = None
        self.in_rate = RATE
        self.ep = Endpointer(self.cfg)
        self.assembler = FrameAssembler(FRAME_BYTES)
        self.stt_task: asyncio.Task | None = None
        self.stt_version = -1
        self.resp_task: asyncio.Task | None = None
        self.responding = False
        self.turn_idx = 0
        self.m: dict = {}
        self.closed = False
        self.ignored_frames_while_responding = 0

    # ── session.start ─────────────────────────────────────────────────────
    async def _read_start(self) -> dict:
        try:
            raw = await asyncio.wait_for(self.ws.recv(), self.cfg.session_start_timeout_s)
        except asyncio.TimeoutError:
            raise ProtocolError("no session.start within timeout")
        if isinstance(raw, (bytes, bytearray)):
            raise ProtocolError("first message was binary, expected session.start JSON")
        try:
            start = json.loads(raw)
        except ValueError:
            raise ProtocolError("session.start is not valid JSON")
        if not isinstance(start, dict) or start.get("type") != "session.start":
            raise ProtocolError(f"expected session.start, got {str(start)[:80]}")
        audio = start.get("audio") or {}
        enc = audio.get("encoding", "pcm_s16le")
        ch = int(audio.get("channels", 1) or 1)
        if enc != "pcm_s16le" or ch != 1:
            raise ProtocolError(f"unsupported audio format encoding={enc} channels={ch}")
        self.in_rate = int(audio.get("sample_rate", RATE) or RATE)
        if self.in_rate != RATE:
            log.warning("[%s] sample_rate=%s, resampling to/from 16 kHz", start.get("session_id"), self.in_rate)
        return start

    async def run(self):
        try:
            start = await self._read_start()
        except ProtocolError as e:
            log.error("protocol error: %s", e)
            await self.ws.close(code=1002, reason=str(e)[:100])
            return
        except Exception as e:  # connection dropped before start
            log.warning("connection closed before session.start: %r", e)
            return
        self.sid = str(start.get("session_id") or "no-session-id")
        self.case_id = start.get("case_id")
        self.tools = ToolsClient(self.app.http, start.get("tools_url"), self.sid, self.cfg.tool_timeout_s)
        tools_task = asyncio.create_task(self.tools.load())
        self.dialog = Dialog(self.app.llm, self.tools,
                             policy.build_static_prompt(self.app.prompt_template, self.app.phrases,
                                                        getattr(self.app, "rules", None), start.get("now")),
                             self.cfg, self.app.phrases,
                             gate=Gate.from_config(self.cfg, start) if self.cfg.tool_gate else None,
                             context=policy.build_context(start))
        self.tools_ready = tools_task
        log.info("[%s] session.start case=%s tools_url=%s caller=%s history=%d",
                 self.sid, self.case_id, start.get("tools_url"), bool(start.get("caller")),
                 len(start.get("history") or []))
        try:
            async for msg in self.ws:
                if isinstance(msg, (bytes, bytearray)):
                    if self.in_rate != RATE:
                        msg = resample(bytes(msg), self.in_rate, RATE)
                    for frame in self.assembler.push(msg):
                        self._on_frame(frame)
                else:
                    self._on_text(msg)
        except Exception as e:
            log.info("[%s] connection ended: %s", self.sid, type(e).__name__)
        finally:
            self.closed = True
            for t in (self.stt_task, self.resp_task, tools_task):
                if t and not t.done():
                    t.cancel()

    def _on_text(self, msg: str):
        try:
            e = json.loads(msg)
        except ValueError:
            log.warning("[%s] ignored non-JSON text message", self.sid)
            return
        log.info("[%s] text event %s (ignored)", self.sid, e.get("type") if isinstance(e, dict) else type(e).__name__)

    # ── endpointing ───────────────────────────────────────────────────────
    def _on_frame(self, frame: bytes):
        now = time.perf_counter()
        if self.responding:
            # Half-duplex baseline: no barge-in while the agent is answering.
            self.ignored_frames_while_responding += 1
            return
        ev = self.ep.feed(frame)
        if ev == "start":
            self.m = {"t_speech_start": now}
        if not self.ep.active:
            return
        if ev == "resume":
            self.m["resumes"] = self.m.get("resumes", 0) + 1
            if self.stt_task:
                self.stt_task.cancel()
                self.stt_task = None
        sil = self.ep.silence_ms
        if sil == 0:
            self.m["t_last_speech"] = now
        if sil >= self.cfg.stt_trigger_silence_ms and self.stt_task is None:
            self.stt_version = self.ep.version
            self.m["t_stt_start"] = now
            self.stt_task = asyncio.create_task(self._stt(self.ep.utterance_audio()))

        reason = None
        if self.ep.utt_ms >= self.cfg.max_utterance_ms:
            reason = "max_utterance"
        elif sil >= self.cfg.max_silence_ms:
            reason = "max_silence"
        elif self.cfg.semantic_eou and sil >= self.cfg.complete_silence_ms and self.stt_task and self.stt_task.done() \
                and not self.stt_task.cancelled() and self.stt_task.exception() is None:
            res = self.stt_task.result()
            c = looks_complete(res.text)
            self.m["completeness"] = c
            if c is True:
                reason = "complete_text"
        if reason:
            self._commit(reason, now)

    async def _stt(self, pcm: bytes):
        res = await self.app.stt.transcribe(pcm, {"case_id": self.case_id, "session_id": self.sid})
        self.m["t_stt_end"] = time.perf_counter()
        self.m["stt_ms"] = round(res.ms, 1)
        return res

    def _commit(self, reason: str, now: float):
        self.turn_idx += 1
        self.m.update(t_commit=now, eou_reason=reason, utt_ms=self.ep.utt_ms, silence_at_commit_ms=self.ep.silence_ms)
        audio = self.ep.utterance_audio()
        stt_task = self.stt_task if (self.stt_task and self.stt_version == self.ep.version) else None
        if stt_task is None and self.stt_task:
            self.stt_task.cancel()
        self.stt_task = None
        self.ep.reset()
        self.responding = True
        self.resp_task = asyncio.create_task(self._respond(stt_task, audio, self.m))

    # ── response ──────────────────────────────────────────────────────────
    async def _respond(self, stt_task, audio: bytes, m: dict):
        spoken = {"n": 0}
        user_text, reply = "", []
        try:
            try:
                async with asyncio.timeout(self.cfg.turn_budget_s):
                    if stt_task is None:
                        m["t_stt_start"] = time.perf_counter()
                        stt_task = asyncio.create_task(self._stt(audio))
                    res = await stt_task
                    m["t_stt_ready"] = time.perf_counter()
                    user_text = res.text
                    if not user_text.strip():
                        m["outcome"] = "not_heard"
                        await self._speak_cached("not_heard", m, spoken)
                        return
                    if not self.tools_ready.done():
                        await self.tools_ready
                    m["tools_available"] = sorted(self.tools.names)
                    if self.tools.list_error:
                        m["tools_list_error"] = self.tools.list_error
                    await self._pipeline(self.dialog.run_turn(user_text, m), m, spoken, reply)
                    m["outcome"] = "ok"
            except TimeoutError:
                m["outcome"] = "budget_timeout"
                log.warning("[%s] turn budget %.1fs exceeded", self.sid, self.cfg.turn_budget_s)
                if not spoken["n"]:
                    await self._speak_cached("delay_fallback", m, spoken)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                m["outcome"] = f"error:{type(e).__name__}"
                log.exception("[%s] turn failed", self.sid)
                if not spoken["n"]:
                    await self._speak_cached("error_fallback", m, spoken)
        finally:
            await self._send_json({"type": "turn.end"})
            m["t_turn_end"] = time.perf_counter()
            self.responding = False
            self._write_metrics(m, user_text, " ".join(reply))

    async def _pipeline(self, sentences, m, spoken, reply):
        """Producer (LLM sentences) and consumer (TTS + send) overlap, so the next sentence
        is generated while the current one is synthesized."""
        q: asyncio.Queue = asyncio.Queue()

        async def produce():
            try:
                async for s in sentences:
                    await q.put(s)
            finally:
                await q.put(None)

        prod = asyncio.create_task(produce())
        try:
            while True:
                s = await q.get()
                if s is None:
                    break
                reply.append(s)
                before = spoken["n"]
                await self._speak_text(s, m, spoken)
                if isinstance(s, Ack):  # a "checking..." phrase is not an answer: fallbacks must still play
                    spoken["n"] = before
            await prod  # re-raise producer errors
        finally:
            if not prod.done():
                prod.cancel()

    async def _speak_text(self, text: str, m: dict, spoken: dict):
        """tts_ms = time until the first audio of this sentence is available
        (whole clip for non-streaming providers, first chunk for streaming ones)."""
        t = time.perf_counter()
        cached = getattr(self.app, "cached_text", {}).get(text)
        if cached is not None:  # pre-synthesized at startup (tool_ack): no TTS wait
            m.setdefault("tts_ms", []).append(0.0)
            await self._send_audio(text, cached, m, spoken)
            return
        norm = normalize_for_tts(text)
        if hasattr(self.app.tts, "stream"):
            first = True
            async for pcm in self.app.tts.stream(norm):
                if first:
                    m.setdefault("tts_ms", []).append(round((time.perf_counter() - t) * 1000, 1))
                    if self.cfg.send_transcript_event:
                        await self._send_json({"type": "agent.transcript", "text": text})
                    first = False
                    spoken["n"] += 1  # counted at first chunk: a mid-stream failure must not add a 2nd reply
                await self._send_pcm(pcm, m)
            return
        pcm = await self.app.tts.synth(norm)
        m.setdefault("tts_ms", []).append(round((time.perf_counter() - t) * 1000, 1))
        await self._send_audio(text, pcm, m, spoken)

    async def _speak_cached(self, key: str, m: dict, spoken: dict):
        pcm = self.app.cached.get(key)
        if pcm is None:
            pcm = await self.app.tts.synth(self.app.phrases[key])
        await self._send_audio(self.app.phrases[key], pcm, m, spoken)

    async def _send_audio(self, text: str, pcm: bytes, m: dict, spoken: dict):
        if self.cfg.send_transcript_event:
            await self._send_json({"type": "agent.transcript", "text": text})
        await self._send_pcm(pcm, m)
        spoken["n"] += 1

    async def _send_pcm(self, pcm: bytes, m: dict):
        if not m.get("_voiced_started"):
            # drop silence before the first voiced sample of the turn (time-to-first-sound)
            pcm = trim_leading_silence(pcm)
            if not pcm:
                return
            m["_voiced_started"] = True
        if self.in_rate != RATE:
            pcm = resample(pcm, RATE, self.in_rate)
        size = self.in_rate * 2 * self.cfg.output_chunk_ms // 1000
        t0 = time.perf_counter()
        for i, c in enumerate(chunk(pcm, size)):
            if self.closed:
                return
            await self.ws.send(c)
            if i == 0 and "t_first_audio" not in m:
                m["t_first_audio"] = time.perf_counter()
            if self.cfg.output_pacing == "realtime":
                wait = t0 + (i + 1) * self.cfg.output_chunk_ms / 1000 - time.perf_counter()
                if wait > 0:
                    await asyncio.sleep(wait)

    async def _send_json(self, obj: dict):
        if self.closed:
            return
        try:
            await self.ws.send(json.dumps(obj, ensure_ascii=False))
        except Exception as e:
            log.info("[%s] send failed: %s", self.sid, type(e).__name__)

    # ── metrics (timings only; transcripts only if AGENT_LOG_TEXT=1) ───────
    def _write_metrics(self, m: dict, user_text: str, reply: str):
        base = m.get("t_last_speech") or m.get("t_commit")

        def rel(k):
            return round((m[k] - base) * 1000, 1) if k in m and base else None

        row = {
            "session_id": self.sid, "case_id": self.case_id, "turn": self.turn_idx,
            "eou_reason": m.get("eou_reason"), "completeness": m.get("completeness"),
            "resumes": m.get("resumes", 0), "utt_ms": m.get("utt_ms"),
            # all *_ms_rel are measured from the arrival of the last voiced frame
            "commit_ms_rel": rel("t_commit"), "stt_start_ms_rel": rel("t_stt_start"),
            "stt_end_ms_rel": rel("t_stt_end"), "stt_ms": m.get("stt_ms"),
            "llm_first_text_ms_rel": rel("t_llm_first_text"),
            "first_audio_ms_rel": rel("t_first_audio"), "turn_end_ms_rel": rel("t_turn_end"),
            "llm_rounds": m.get("llm_rounds"), "tool_calls": m.get("tool_calls"),
            "tools_available": m.get("tools_available"), "tools_list_error": m.get("tools_list_error"),
            "tts_ms": m.get("tts_ms"), "handoff_flags": m.get("handoff_flags"),
            "outcome": m.get("outcome"), "ignored_frames_while_responding": self.ignored_frames_while_responding,
            "providers": {"stt": self.app.stt.name, "llm": self.app.llm.name, "tts": self.app.tts.name},
        }
        if self.cfg.log_text:
            row["user_text"], row["reply_text"] = user_text, reply
        self.app.write_metrics(row)
        log.info("[%s] turn %d %s commit=%sms first_audio=%sms turn_end=%sms outcome=%s",
                 self.sid, self.turn_idx, row["eou_reason"], row["commit_ms_rel"], row["first_audio_ms_rel"],
                 row["turn_end_ms_rel"], row["outcome"])
