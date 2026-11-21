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

resume_before_audio (opt-in, eou_mode=vad only): after a commit the session keeps feeding the
VAD until the first voiced audio of the reply is sent. If the customer starts speaking again
(min_speech_ms onset), the prepared reply (STT / LLM / TTS) is cancelled, the dialog history
of that turn is rolled back, and the previous utterance + the new audio are endpointed again as
one utterance. Not done once a function call was sent to tools_url this turn (it would be
recorded twice), nor after the first sound (no barge-in).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from . import policy
from .audio import FRAME_BYTES, FRAME_MS, RATE, FrameAssembler, chunk, resample, trim_leading_silence
from .dialog import Ack, Dialog
from .endpointing import Endpointer
from .gate import Gate
from .prefetch import Prefetch
from .providers.stt import STTResult
from .soniox_stream import SonioxStream
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
        # Optional debug sink (callable taking a dict). None on the competition server path
        # (team.agent.server); only team.devui sets it. When None nothing extra is sent.
        self.debug = None
        self.t0 = time.perf_counter()
        self.stream: SonioxStream | None = None  # only with cfg.eou_mode == "soniox_stream"
        self.stream_end: str | None = None       # text of a "<end>" not acted on yet
        # resume_before_audio: committed utterance audio and frames heard since the commit
        self.resp_prev_audio = b""
        self.resp_heard = bytearray()
        self.resp_listen = False
        self.cancelled_task: asyncio.Task | None = None  # reply cancelled by a resume, may still be unwinding

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
        self.t0 = time.perf_counter()
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
        if self.cfg.prefetch:  # order / return lookups called ahead (prefetch.py), never registrations
            pf = Prefetch(self.tools, getattr(self.app, "rules", None), start.get("history"), start.get("caller"),
                          max_calls=self.cfg.prefetch_max_calls)
            self.dialog.prefetch = pf

            async def prefetch_at_start():
                await asyncio.shield(tools_task)  # only functions listed by GET /tools are ever sent
                pf.session_start()
            self.prefetch_task = asyncio.create_task(prefetch_at_start())
        if self.debug:
            self.tools.debug = self._dbg
            self.dialog.debug = self._dbg
            tools_task.add_done_callback(lambda _t: self._dbg(
                "tools", names=sorted(self.tools.names), error=self.tools.list_error))
            self._dbg("session", session_id=self.sid, case_id=self.case_id, tools_url=start.get("tools_url"),
                      history_len=len(start.get("history") or []), caller=bool(start.get("caller")),
                      in_rate=self.in_rate)
        log.info("[%s] session.start case=%s tools_url=%s caller=%s history=%d",
                 self.sid, self.case_id, start.get("tools_url"), bool(start.get("caller")),
                 len(start.get("history") or []))
        if self.cfg.eou_mode == "soniox_stream":
            try:
                self.stream = SonioxStream(self.cfg, self._stream_token, self._stream_end, self.sid)
                self.stream.start()
            except Exception as e:  # no key etc. -> local VAD mode for this session
                log.warning("[%s] eou_mode=soniox_stream unavailable (%s); using local VAD", self.sid, e)
                self.stream = None
            self._dbg("eou_mode", mode="soniox_stream" if self.stream else "vad (fallback)")
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
            for t in (self.stt_task, self.resp_task, tools_task, getattr(self, "prefetch_task", None)):
                if t and not t.done():
                    t.cancel()
            if getattr(self, "dialog", None) and self.dialog.prefetch:
                self.dialog.prefetch.cancel()
            if self.stream:
                await self.stream.close()

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
        if self.stream:
            self.stream.send(frame)  # also while responding: keeps the Soniox stream fed
        if self.responding:
            if self.resp_listen and self._listen_before_audio(frame, now):
                return
            # Half-duplex baseline: no barge-in while the agent is answering.
            self.ignored_frames_while_responding += 1
            return
        ev = self.ep.feed(frame)
        if ev == "start":
            self.m = {"t_speech_start": now}
            self._dbg("vad", event="speech_start")
        if not self.ep.active:
            return
        if ev == "resume":
            self.m["resumes"] = self.m.get("resumes", 0) + 1
            self._dbg("vad", event="resume", resumes=self.m["resumes"])
            if self.stt_task:
                self.stt_task.cancel()
                self.stt_task = None
        sil = self.ep.silence_ms
        if sil == 0:
            self.m["t_last_speech"] = now
        if self.stream and self.stream.ok:
            self._on_frame_stream(sil, now)
            return
        if sil >= self.cfg.stt_trigger_silence_ms and self.stt_task is None:
            self.stt_version = self.ep.version
            self.m["t_stt_start"] = now
            self.stt_task = asyncio.create_task(self._stt(self.ep.utterance_audio()))
            self._dbg("vad", event="speculative_stt_start", silence_ms=sil)

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

    # ── resume_before_audio (opt-in) ──────────────────────────────────────
    def _listen_before_audio(self, frame: bytes, now: float) -> bool:
        """Frame heard while the reply is being prepared. Returns False when listening has
        ended for this turn (the frame is then ignored as before)."""
        m = self.m
        if m.get("_out_started") or self.resp_task is None or self.resp_task.done():
            self._stop_listening()
            return False
        if self.tools.sent_count > m.get("_tools_sent_at_commit", 0):
            # a function call already reached the function server: cancelling could make the
            # next turn send it again (e.g. a second registration)
            m["resume_blocked"] = "tool_sent"
            self._stop_listening()
            return False
        self.resp_heard.extend(frame)
        if self.ep.feed(frame) != "start":
            return True
        # the customer goes on speaking: cancel the prepared reply, continue the same utterance
        old = self.m
        old["_resumed"] = True
        self.cancelled_task = self.resp_task
        self.resp_task.cancel()
        self.resp_task = None
        self.responding = False
        self.turn_idx -= 1  # the cancelled reply was never a turn
        n = old.get("resumed_after_commit", 0) + 1
        self.m = {"t_speech_start": old.get("t_speech_start", now), "resumes": old.get("resumes", 0),
                  "resumed_after_commit": n, "t_last_speech": now}
        utt = self.resp_prev_audio + bytes(self.resp_heard)  # the VAD preroll is the tail of resp_heard
        self.ep.utt = bytearray(utt)
        self.ep.utt_ms = len(utt) * 1000 // (RATE * 2)
        self.ep.last_speech_end_bytes = len(utt)
        self.ep.silence_ms = 0
        self.resp_listen = False  # not _stop_listening(): the VAD keeps this utterance open
        self.resp_prev_audio, self.resp_heard = b"", bytearray()
        log.info("[%s] speech resumed %dms after commit: reply cancelled (%d)", self.sid,
                 round((now - old.get("t_commit", now)) * 1000), n)
        self._dbg("vad", event="resumed_after_commit", count=n,
                  after_commit_ms=round((now - old.get("t_commit", now)) * 1000, 1))
        return True

    def _stop_listening(self):
        if self.resp_listen:
            self.resp_listen = False
            self.resp_prev_audio, self.resp_heard = b"", bytearray()
            self.ep.reset()  # frames heard while listening are not the next utterance

    # ── eou_mode=soniox_stream (opt-in) ───────────────────────────────────
    def _on_frame_stream(self, sil: int, now: float):
        reason = None
        if self.ep.utt_ms >= self.cfg.max_utterance_ms:
            reason = "max_utterance"
        elif sil >= self.cfg.soniox_fallback_silence_ms:
            reason = "fallback_silence"  # no "<end>" from Soniox: batch STT on the local audio
        elif self.stream_end is not None and sil >= self.cfg.soniox_min_silence_ms:
            reason = "soniox_end"
            text, self.stream_end = self.stream_end, None
            if text.strip():
                if self.stt_task:  # speculative STT from before the stream connected
                    self.stt_task.cancel()
                fut = asyncio.get_running_loop().create_future()
                fut.set_result(STTResult(text, 0.0, {"source": "soniox_stream"}))
                self.stt_task, self.stt_version = fut, self.ep.version
                self.m["stt_source"] = "soniox_stream"
            # empty text: no stt_task -> _respond runs batch STT on the utterance audio
        if reason:
            self.stream_end = None
            self.stream.clear()
            self._commit(reason, now)

    def _stream_end(self, text: str):
        if self.responding or not self.ep.active:
            self._dbg("soniox", event="end_ignored", text=text, responding=self.responding)
            return
        self.stream_end = text
        self.m["t_soniox_end"] = time.perf_counter()
        self._dbg("soniox", event="end", text=text, silence_ms=self.ep.silence_ms)

    def _stream_token(self, _t: str):
        if self.stream_end is not None and not self.responding:
            self.stream_end = None  # the customer went on speaking after "<end>"
            self.m["resumes"] = self.m.get("resumes", 0) + 1
            self._dbg("soniox", event="resume")

    async def _stt(self, pcm: bytes):
        res = await self.app.stt.transcribe(pcm, {"case_id": self.case_id, "session_id": self.sid})
        self.m["t_stt_end"] = time.perf_counter()
        self.m["stt_ms"] = round(res.ms, 1)
        self._dbg("stt", text=res.text, ms=round(res.ms, 1))
        return res

    def _commit(self, reason: str, now: float):
        self.turn_idx += 1
        self.m.update(t_commit=now, eou_reason=reason, utt_ms=self.ep.utt_ms, silence_at_commit_ms=self.ep.silence_ms)
        audio = self.ep.utterance_audio()
        if self.cfg.resume_before_audio and self.cfg.eou_mode == "vad" and self.stream is None:
            self.resp_listen = True
            self.resp_prev_audio, self.resp_heard = bytes(self.ep.utt), bytearray()
            tools = getattr(self, "tools", None)
            self.m["_tools_sent_at_commit"] = tools.sent_count if tools else 0
        stt_task = self.stt_task if (self.stt_task and self.stt_version == self.ep.version) else None
        if stt_task is None and self.stt_task:
            self.stt_task.cancel()
        self.stt_task = None
        self.ep.reset()
        self._dbg("eou", reason=reason, utt_ms=self.m.get("utt_ms"), silence_ms=self.m.get("silence_at_commit_ms"),
                  completeness=self.m.get("completeness"), reuse_speculative_stt=stt_task is not None,
                  t_last_speech_ms=self._rel(self.m.get("t_last_speech")), t_commit_ms=self._rel(now))
        self.responding = True
        self.resp_task = asyncio.create_task(self._respond(stt_task, audio, self.m))

    # ── response ──────────────────────────────────────────────────────────
    async def _respond(self, stt_task, audio: bytes, m: dict):
        spoken = {"n": 0}
        user_text, reply = "", []
        prev, self.cancelled_task = self.cancelled_task, None
        if prev is not None and not prev.done():  # let a cancelled reply roll back its history first
            await asyncio.wait([prev])
        hist_len = len(self.dialog.messages)
        try:
            try:
                async with asyncio.timeout(self.cfg.turn_budget_s):
                    if stt_task is None:
                        m["t_stt_start"] = time.perf_counter()
                        stt_task = asyncio.create_task(self._stt(audio))
                    res = await stt_task
                    m["t_stt_ready"] = time.perf_counter()
                    user_text = res.text
                    self._dbg("stt.final", text=user_text)
                    if not user_text.strip():
                        m["outcome"] = "not_heard"
                        await self._speak_cached("not_heard", m, spoken)
                        return
                    if not self.tools_ready.done():
                        await asyncio.shield(self.tools_ready)  # a cancelled turn must not cancel the load
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
                if m.get("_resumed"):
                    # cancelled by resume_before_audio: nothing was said, the utterance continues
                    del self.dialog.messages[hist_len:]
                    self._dbg("reply_cancelled", reason="resumed_after_commit")
                raise
            except Exception as e:
                m["outcome"] = f"error:{type(e).__name__}"
                log.exception("[%s] turn failed", self.sid)
                if not spoken["n"]:
                    await self._speak_cached("error_fallback", m, spoken)
        finally:
            if not m.get("_resumed"):  # a resumed turn sends no turn.end and writes no metrics row
                self._stop_listening()
                await self._send_json({"type": "turn.end"})
                m["t_turn_end"] = time.perf_counter()
                self._dbg("turn_end_sent", outcome=m.get("outcome"))
                self.responding = False
                if self.stream:
                    self.stream.clear()  # tokens heard while the agent spoke are not the next utterance
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
                # wait until it has stopped, so it cannot touch the dialog history afterwards
                with contextlib.suppress(BaseException):
                    await asyncio.gather(prod, return_exceptions=True)

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
                    m["_out_started"] = True  # from here on the reply is no longer cancelled
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
        m["_out_started"] = True  # the transcript goes out right before the audio
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
            m["_voiced_started"] = m["_out_started"] = True
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
                self._dbg("audio.first_sent")
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

    # ── debug events (team.devui only) ─────────────────────────────────────
    def _rel(self, t):
        return round((t - self.t0) * 1000, 1) if t else None

    def _dbg(self, kind: str, **data):
        """Hand a debug event to the debug sink. No-op unless self.debug is set (devui path)."""
        if not self.debug:
            return
        try:
            self.debug({"type": "debug." + kind, "t_ms": self._rel(time.perf_counter()), **data})
        except Exception as e:  # a debug sink must never break a turn
            log.debug("[%s] debug sink failed: %s", self.sid, type(e).__name__)

    # ── metrics (timings only; transcripts only if AGENT_LOG_TEXT=1) ───────
    def _write_metrics(self, m: dict, user_text: str, reply: str):
        base = m.get("t_last_speech") or m.get("t_commit")

        def rel(k):
            return round((m[k] - base) * 1000, 1) if k in m and base else None

        row = {
            "session_id": self.sid, "case_id": self.case_id, "turn": self.turn_idx,
            "eou_reason": m.get("eou_reason"), "completeness": m.get("completeness"),
            "eou_mode": "soniox_stream" if self.stream else "vad", "stt_source": m.get("stt_source", "batch"),
            "soniox_end_ms_rel": rel("t_soniox_end"),
            "resumes": m.get("resumes", 0), "utt_ms": m.get("utt_ms"),
            "resumed_after_commit": m.get("resumed_after_commit", 0), "resume_blocked": m.get("resume_blocked"),
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
            # what the code checks decided (gate: name / allowed / reason only, no arguments);
            # output-check values (the numbers / time words) only with log_text
            "gate": m.get("gate"), "blocked_reply": m.get("blocked_reply"),
            "prefetch": (self.dialog.prefetch.summary()
                         if getattr(self, "dialog", None) and self.dialog.prefetch else None),
            "prefetch_given": m.get("prefetch_given"), "prefetch_wait_ms": m.get("prefetch_wait_ms"),
            "write_answer_ahead": m.get("write_answer_ahead"),
            "output_flags": [f if self.cfg.log_text else {k: v for k, v in f.items() if k != "values"}
                             for f in m.get("output_flags") or []],
        }
        if self.cfg.log_text:
            row["user_text"], row["reply_text"] = user_text, reply
        self.app.write_metrics(row)
        # texts go to the debug client only (never to the metrics file unless log_text)
        self._dbg("metrics", row=row, user_text=user_text, reply_text=reply)
        log.info("[%s] turn %d %s commit=%sms first_audio=%sms turn_end=%sms outcome=%s",
                 self.sid, self.turn_idx, row["eou_reason"], row["commit_ms_rel"], row["first_audio_ms_rel"],
                 row["turn_end_ms_rel"], row["outcome"])
