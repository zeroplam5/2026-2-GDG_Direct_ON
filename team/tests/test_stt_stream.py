"""AGENT_STT_STREAM=1: Soniox transcribes while the customer speaks, the local VAD decides the end.

No key, no network: FakeStream (below) replaces SonioxStream in the session; it "hears" the voiced
segments of the frames it is sent and returns one scripted part per segment. The real SonioxStream
is checked against a local fake WebSocket server (finalize / "<fin>", non-final tokens, clear()).
Says nothing about real Soniox latency or accuracy."""
import asyncio
import json

import numpy as np
import pytest
import websockets

from team.agent.config import Config
from team.agent.providers.stt import STTResult
from team.agent.soniox_stream import SonioxStream

from .helpers import FRAME, RATE, run_client, silence, speechlike
from .test_agent_protocol import _metrics, agent, run, two_mocks  # noqa: F401  (fixtures)

PART1 = "배송 문의 드릴게요."
PART2 = "주문번호 DEMO1001 배송 어디쯤이에요?"
BATCH = "일괄 받아쓰기 글"


class FakeStream:
    """TEST DOUBLE for SonioxStream. A new voiced segment (after >= 0.6 s quiet) makes the next
    scripted part the non-final text; finalize() makes it final; after 0.5 s quiet it sends
    "<end>" (which stt_stream must ignore)."""
    parts: list = []
    fin_mode = "fin"          # fin | ignore
    connect = True            # False: never connects
    fail_after_frames = None  # error after this many frames
    made: list = []

    def __init__(self, cfg, on_token, on_end, sid=""):
        self.on_token, self.on_end = on_token, on_end
        self.final, self.partial = [], ""
        self.connected, self.error, self.closed = False, None, False
        self.frames, self.segs, self.quiet, self.inside = 0, 0, 0, False
        self.finalizes, self.ends, self.clears = [], 0, 0
        FakeStream.made.append(self)

    @property
    def ok(self):
        return self.connected and self.error is None and not self.closed

    def start(self):
        self.connected = FakeStream.connect

    def send(self, frame):
        self.frames += 1
        if self.fail_after_frames is not None and self.frames >= self.fail_after_frames:
            self.error = "ConnectionClosed: test"
        x = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        loud = np.sqrt(np.mean(x ** 2)) > 500
        if loud:
            if not self.inside or self.quiet >= 30:
                if self.segs < len(self.parts):
                    self.partial = (" " if self.final or self.partial else "") + self.parts[self.segs]
                self.segs += 1
            self.inside, self.quiet = True, 0
        else:
            self.quiet += 1
            if self.inside and self.quiet == 25:
                self.final.append(self.partial)
                self.partial = ""
                self.ends += 1
                self.on_end(self.text())

    def text(self):
        return ("".join(self.final) + self.partial).strip()

    def clear(self):
        self.final, self.partial = [], ""
        self.clears += 1

    async def finalize(self, timeout_s):
        self.finalizes.append(self.text())
        if FakeStream.fin_mode == "ignore":
            await asyncio.sleep(timeout_s)
            return "timeout"
        if not self.ok:
            return "error"
        if self.partial:
            self.final.append(self.partial)
            self.partial = ""
        return "fin"

    async def close(self):
        self.closed = True


class CountingSTT:
    """Batch STT test double: counts calls, always returns BATCH."""
    name = "counting_fake"

    def __init__(self, text=BATCH, delay_s=0.05, exc=None):
        self.text, self.delay_s, self.exc, self.calls = text, delay_s, exc, 0

    async def transcribe(self, pcm, ctx=None):
        self.calls += 1
        await asyncio.sleep(self.delay_s)
        if self.exc:
            raise self.exc
        return STTResult(self.text, self.delay_s * 1000, {})


@pytest.fixture
def fake_stream(monkeypatch):
    monkeypatch.setenv("SONIOX_API_KEY", "test-key")  # App builds SonioxSTT (replaced by CountingSTT)
    monkeypatch.setattr("team.agent.session.SonioxStream", FakeStream)
    monkeypatch.setattr(FakeStream, "made", [])
    monkeypatch.setattr(FakeStream, "parts", [PART1, PART2])
    monkeypatch.setattr(FakeStream, "fin_mode", "fin")
    monkeypatch.setattr(FakeStream, "connect", True)
    monkeypatch.setattr(FakeStream, "fail_after_frames", None)
    return FakeStream


def _on(cfg, **kw):
    cfg.stt_provider, cfg.stt_stream, cfg.log_text = "soniox", True, True
    for k, v in kw.items():
        setattr(cfg, k, v)


def _go(start, sid, pcm, murl, stt=None, speed=4):
    async def go():
        h = await start()
        h["app"].stt = stt or CountingSTT()
        r = await run_client(h["url"], sid, pcm, murl, case_id="STREAM", speed=speed)
        return r, h["app"].stt
    return run(go())


def _row(cfg, sid):
    rows = [x for x in _metrics(cfg) if x["session_id"] == sid]
    assert len(rows) == 1
    return rows[0]


# ── settings ────────────────────────────────────────────────────────────────
def test_off_by_default_and_read_from_env(monkeypatch):
    c = Config()
    assert c.stt_stream is False and c.stream_finalize_ms == 700 and c.stt_timeout_s == 12.0
    monkeypatch.setenv("AGENT_STT_STREAM", "1")
    monkeypatch.setenv("AGENT_STREAM_FINALIZE_MS", "500")
    c = Config.from_env()
    assert c.stt_stream is True and c.stream_finalize_ms == 500


def test_off_opens_no_stream_and_uses_batch(agent, two_mocks, fake_stream):
    start, cfg = agent
    cfg.stt_provider, cfg.log_text = "soniox", True
    (_, murl), _ = two_mocks
    r, stt = _go(start, "SID-STR-OFF", speechlike(1.5), murl)
    assert r["turn_end"] and fake_stream.made == [] and stt.calls >= 1
    row = _row(cfg, "SID-STR-OFF")
    assert row["stt_source"] == "batch" and row["stt_stream"] is False and row["user_text"] == BATCH


def test_needs_soniox_provider(agent, two_mocks, fake_stream):
    start, cfg = agent
    _on(cfg, stt_provider="fake")
    (_, murl), _ = two_mocks
    r, stt = _go(start, "SID-STR-PROV", speechlike(1.5), murl)
    assert r["turn_end"] and fake_stream.made == [] and stt.calls >= 1


# ── on ──────────────────────────────────────────────────────────────────────
def test_vad_decides_end_stream_text_is_used_no_batch(agent, two_mocks, fake_stream):
    start, cfg = agent
    _on(cfg, semantic_eou=False)
    fake_stream.parts = [PART2]
    (store, murl), _ = two_mocks
    r, stt = _go(start, "SID-STR-ON", speechlike(1.5), murl)
    assert r["turn_end"] and "배송중" in r["text"]
    assert stt.calls == 0                                   # no speculative, no batch STT
    s = fake_stream.made[0]
    assert s.ends == 1                                      # "<end>" came ...
    row = _row(cfg, "SID-STR-ON")
    assert row["eou_reason"] == "max_silence"               # ... but the VAD decided the end
    assert row["eou_mode"] == "vad" and row["stt_stream"] is True
    assert row["stt_source"] == "stream" and row["stream_finalize"] == "fin"
    assert row["user_text"] == PART2
    assert row["stt_ready_ms_rel"] is not None and row["stt_ready_ms_rel"] >= row["commit_ms_rel"]
    assert row["outcome"] == "ok"
    assert [c["name"] for c in store.calls_for("SID-STR-ON")] == ["get_order_info"]
    assert s.frames >= len(speechlike(1.5)) // FRAME        # every frame went to the stream


def test_end_token_does_not_commit(agent, two_mocks, fake_stream):
    """"<end>" arrives 0.5 s after the speech; with no complete-text rule the commit still waits
    for max_silence (soniox_stream mode would commit at soniox_min_silence_ms = 300 ms)."""
    start, cfg = agent
    _on(cfg, semantic_eou=False, max_silence_ms=1500)
    (_, murl), _ = two_mocks
    r, _ = _go(start, "SID-STR-END", speechlike(1.0), murl)
    row = _row(cfg, "SID-STR-END")
    assert fake_stream.made[0].ends == 1
    assert row["eou_reason"] == "max_silence" and row["eou_mode"] == "vad"


def test_completeness_check_reads_stream_text(agent, two_mocks, fake_stream):
    """semantic_eou keeps working without speculative STT: the stream text is checked."""
    start, cfg = agent
    _on(cfg)
    fake_stream.parts = [PART2]                              # ends with "?" -> looks complete
    (_, murl), _ = two_mocks
    r, stt = _go(start, "SID-STR-SEM", speechlike(1.5), murl)
    row = _row(cfg, "SID-STR-SEM")
    assert row["eou_reason"] == "complete_text" and row["completeness"] is True and stt.calls == 0


def test_finalize_ignored_waits_then_uses_text_so_far(agent, two_mocks, fake_stream):
    start, cfg = agent
    _on(cfg, stream_finalize_ms=200)
    fake_stream.fin_mode = "ignore"
    fake_stream.parts = [PART2]
    (_, murl), _ = two_mocks
    r, stt = _go(start, "SID-STR-IGN", speechlike(1.5), murl)
    row = _row(cfg, "SID-STR-IGN")
    assert row["stream_finalize"] == "timeout" and row["stt_source"] == "stream" and row["user_text"] == PART2
    assert stt.calls == 0 and row["stt_ms"] >= 190


def test_resume_gets_both_parts_without_sending_audio_again(agent, two_mocks, fake_stream):
    from .test_resume_before_audio import PART2_END_FRAME, PCM
    start, cfg = agent
    _on(cfg, resume_before_audio=True, scripted_delay_ms=1500)
    (store, murl), _ = two_mocks
    r, stt = _go(start, "SID-STR-RES", PCM, murl, speed=1)
    assert r["turn_end"] and [e["type"] for e in r["events"]].count("turn.end") == 1
    assert "배송중" in r["text"] and r["first_agent_frame_idx"] > PART2_END_FRAME
    s = fake_stream.made[0]
    assert s.finalizes[0] == PART1                          # 1st commit (cancelled reply) ...
    assert s.finalizes[1] == f"{PART1} {PART2}"             # ... 2nd commit has both parts
    assert stt.calls == 0                                   # nothing transcribed again
    assert s.frames <= r["frames_sent"]                     # no audio sent twice to the stream
    row = _row(cfg, "SID-STR-RES")
    assert row["user_text"] == f"{PART1} {PART2}" and row["resumed_after_commit"] == 1
    assert row["stt_source"] == "stream" and row["outcome"] == "ok"
    assert [c["name"] for c in store.calls_for("SID-STR-RES")] == ["get_order_info"]


async def _two_turn_client(url, sid, tools_url, pcms, speed=4.0):
    events = []
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "session.start", "session_id": sid, "case_id": "STREAM",
                                  "now": "2026-11-21 11:00", "history": [], "caller": None, "tools_url": tools_url,
                                  "audio": {"encoding": "pcm_s16le", "sample_rate": RATE, "channels": 1}}))
        for pcm in pcms:
            ended = asyncio.Event()

            async def recv():
                async for m in ws:
                    if isinstance(m, str):
                        events.append(json.loads(m))
                        if events[-1]["type"] == "turn.end":
                            ended.set()
                            return
            rt = asyncio.create_task(recv())
            k = 0
            while not ended.is_set() and k < 1500:
                c = pcm[k * FRAME:(k + 1) * FRAME]
                await ws.send(c + b"\0" * (FRAME - len(c)))
                k += 1
                await asyncio.sleep(0.02 / speed)
            await asyncio.wait_for(rt, 5)
            for _ in range(10):  # a little silence between the turns
                await ws.send(b"\0" * FRAME)
    return events


def test_second_turn_text_has_no_first_turn_text(agent, two_mocks, fake_stream):
    start, cfg = agent
    _on(cfg)
    fake_stream.parts = ["주문번호 DEMO1001 조회해 주세요.", "고맙습니다."]
    (_, murl), _ = two_mocks

    async def go():
        h = await start()
        h["app"].stt = CountingSTT()
        ev = await _two_turn_client(h["url"], "SID-STR-TWO", murl,
                                    [speechlike(1.2, seed=1), speechlike(1.0, seed=2)])
        return ev, h["app"].stt
    ev, stt = run(go())
    assert [e["type"] for e in ev].count("turn.end") == 2 and stt.calls == 0
    rows = [x for x in _metrics(cfg) if x["session_id"] == "SID-STR-TWO"]
    assert [x["user_text"] for x in rows] == ["주문번호 DEMO1001 조회해 주세요.", "고맙습니다."]
    assert all(x["stt_source"] == "stream" for x in rows)


# ── fallbacks ───────────────────────────────────────────────────────────────
def test_stream_never_connected_falls_back_to_batch(agent, two_mocks, fake_stream):
    start, cfg = agent
    _on(cfg)
    fake_stream.connect = False
    (_, murl), _ = two_mocks
    r, stt = _go(start, "SID-STR-NOCONN", speechlike(1.5), murl)
    row = _row(cfg, "SID-STR-NOCONN")
    assert r["turn_end"] and stt.calls >= 1                 # speculative / batch STT as before
    assert row["stt_source"] == "batch" and row["user_text"] == BATCH and row["stream_finalize"] is None


def test_stream_failing_mid_utterance_falls_back_to_batch(agent, two_mocks, fake_stream):
    start, cfg = agent
    _on(cfg)
    fake_stream.fail_after_frames = 20
    (_, murl), _ = two_mocks
    r, stt = _go(start, "SID-STR-FAIL", speechlike(1.5), murl)
    row = _row(cfg, "SID-STR-FAIL")
    assert r["turn_end"] and stt.calls >= 1 and row["stt_source"] == "batch" and row["user_text"] == BATCH


def test_empty_stream_text_falls_back_to_batch(agent, two_mocks, fake_stream):
    start, cfg = agent
    _on(cfg)
    fake_stream.parts = []
    (_, murl), _ = two_mocks
    r, stt = _go(start, "SID-STR-EMPTY", speechlike(1.5), murl)
    row = _row(cfg, "SID-STR-EMPTY")
    assert stt.calls == 1 and row["stt_source"] == "batch" and row["user_text"] == BATCH


# ── stt_timeout outcome (also without the stream) ───────────────────────────
def test_stt_timeout_is_its_own_outcome(agent, two_mocks):
    start, cfg = agent
    (_, murl), _ = two_mocks
    r, _ = _go(start, "SID-STT-TO", speechlike(1.0), murl, stt=CountingSTT(exc=asyncio.TimeoutError()))
    row = _metrics(cfg)[-1]
    assert r["turn_end"] and "지연" in r["text"]
    assert row["outcome"] == "stt_timeout"


# ── the real SonioxStream against a local fake server ───────────────────────
def _serve_fake(seen, fin=True):
    async def handler(ws):
        seen["config"] = json.loads(await ws.recv())
        seen["control"], got = [], 0
        async for m in ws:
            if isinstance(m, bytes):
                got += len(m)
                if got == 640 * 5:
                    await ws.send(json.dumps({"tokens": [
                        {"text": "주문", "is_final": True, "start_ms": 0},
                        {"text": "번호", "is_final": False, "start_ms": 40}]}))
                if got == 640 * 6:
                    await ws.send(json.dumps({"tokens": [
                        {"text": "번호는", "is_final": False, "start_ms": 40}]}))
            elif m == "":
                return
            else:
                seen["control"].append(json.loads(m))
                if fin:
                    await ws.send(json.dumps({"tokens": [{"text": "번호는", "is_final": True, "start_ms": 40},
                                                         {"text": "<fin>", "is_final": True}]}))
    return handler


async def _with_stream(seen, fin, body):
    async with websockets.serve(_serve_fake(seen, fin), "127.0.0.1", 0) as srv:
        cfg = Config(soniox_url=f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}", stt_stream=True)
        s = SonioxStream(cfg, lambda *a: None, lambda *a: None, "T")
        s.start()
        try:
            return await body(s)
        finally:
            await s.close()


def test_soniox_stream_final_partial_and_finalize(monkeypatch):
    monkeypatch.setenv("SONIOX_API_KEY", "test-key")
    seen = {}

    async def body(s):
        for _ in range(6):
            s.send(b"\1\0" * 320)
        for _ in range(100):
            if s.partial == "번호는":
                break
            await asyncio.sleep(0.01)
        before = s.text()
        status = await s.finalize(1.0)
        return before, status, s.text(), list(s.final)

    before, status, after, final = asyncio.run(_with_stream(seen, True, body))
    assert before == "주문번호는"                 # final + the LATEST non-final tokens (not appended)
    assert status == "fin" and after == "주문번호는" and final == ["주문", "번호는"]
    assert seen["control"] == [{"type": "finalize"}]


def test_soniox_stream_finalize_ignored_times_out(monkeypatch):
    monkeypatch.setenv("SONIOX_API_KEY", "test-key")

    async def body(s):
        for _ in range(6):
            s.send(b"\1\0" * 320)
        await asyncio.sleep(0.2)
        status = await s.finalize(0.15)
        return status, s.text()

    status, text = asyncio.run(_with_stream({}, False, body))
    assert status == "timeout" and text == "주문번호는"


def test_soniox_stream_clear_drops_late_tokens_of_cleared_speech(monkeypatch):
    monkeypatch.setenv("SONIOX_API_KEY", "test-key")

    async def body(s):
        for _ in range(4):
            s.send(b"\1\0" * 320)    # 80 ms
        await asyncio.sleep(0.1)
        s.clear()                    # tokens of these 80 ms arrive only afterwards
        for _ in range(2):
            s.send(b"\1\0" * 320)
        await asyncio.sleep(0.2)
        return s.text()

    assert asyncio.run(_with_stream({}, True, body)) == ""


def test_soniox_stream_finalize_on_failed_stream_returns_error(monkeypatch):
    monkeypatch.setenv("SONIOX_API_KEY", "test-key")

    async def go():
        cfg = Config(soniox_url="ws://127.0.0.1:9", stt_timeout_s=1.0)
        s = SonioxStream(cfg, lambda *a: None, lambda *a: None, "T")
        s.start()
        await asyncio.sleep(0.3)
        st = await s.finalize(0.2)
        await s.close()
        return st, s.error

    st, err = asyncio.run(go())
    assert st == "error" and err
