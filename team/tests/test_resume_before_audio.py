"""resume_before_audio: after an end-of-utterance commit, keep listening until the first sound
of the reply. The customer says part 1, pauses 1.2 s (long enough for a commit), then says
part 2. Test doubles only (SegmentSTT below, ScriptedLLM with a delay, ToneTTS) - no API keys."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import numpy as np

from team.agent.config import Config
from team.agent.providers.stt import STTResult

from .helpers import FRAME, RATE, run_client, silence, speechlike
from .test_agent_protocol import _metrics, agent, run, two_mocks  # noqa: F401  (fixtures)

PART1 = "배송 문의 드릴게요."                    # looks complete -> committed during the pause
PART2 = "주문번호 DEMO1001 배송 어디쯤이에요?"
ASK_ID = "주문번호를 말씀해"                       # ScriptedLLM's reply to PART1 alone


class SegmentSTT:
    """TEST DOUBLE that does listen: counts the voiced segments in the audio (gaps >= 0.6 s)
    and returns that many of the scripted parts, so a transcript shows which parts it got."""
    name = "segment_fake"

    def __init__(self, parts, delay_s=0.05):
        self.parts, self.delay_s = parts, delay_s

    async def transcribe(self, pcm: bytes, ctx=None) -> STTResult:
        import asyncio
        await asyncio.sleep(self.delay_s)
        x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32)
        win = RATE // 50  # 20 ms
        loud = [np.sqrt(np.mean(x[i:i + win] ** 2)) > 500 for i in range(0, len(x) - win + 1, win)]
        segs, gap, inside = 0, 0, False
        for v in loud:
            if v:
                if not inside or gap >= 30:
                    segs += 1
                inside, gap = True, 0
            else:
                gap += 1
        return STTResult(" ".join(self.parts[:segs]), self.delay_s * 1000, {"segments": segs})


# part 1 (1.0 s) - pause 1.2 s - part 2 (2.0 s): with the option off the reply to part 1
# is ready while part 2 is still being said
PCM = speechlike(1.0, seed=1) + silence(1.2) + speechlike(2.0, seed=2) + silence(0.2)
PART2_END_FRAME = int((1.0 + 1.2 + 2.0) * RATE * 2) // FRAME


def _setup(cfg, resume, llm_delay_ms=1500):
    cfg.resume_before_audio = resume
    cfg.scripted_delay_ms = llm_delay_ms   # slow "LLM": the reply is not ready when part 2 starts
    cfg.log_text = True                    # the test reads the transcript from the metrics row


def _go(start, sid, tools_url):
    async def go():
        h = await start()
        h["app"].stt = SegmentSTT([PART1, PART2])
        return await run_client(h["url"], sid, PCM, tools_url, case_id="RESUME", speed=1)
    return run(go())


def test_option_is_off_by_default_and_read_from_env(monkeypatch):
    assert Config().resume_before_audio is False
    monkeypatch.setenv("AGENT_RESUME_BEFORE_AUDIO", "1")
    assert Config.from_env().resume_before_audio is True


def test_off_answers_part1_only(agent, two_mocks):
    start, cfg = agent
    _setup(cfg, resume=False)
    (store, murl), _ = two_mocks
    r = _go(start, "SID-RES-OFF", murl)
    assert r["turn_end"] and ASK_ID in r["text"] and "배송중" not in r["text"]
    assert r["first_agent_frame_idx"] < PART2_END_FRAME     # talks while part 2 is being said
    row = _metrics(cfg)[-1]
    assert row["user_text"] == PART1 and row["resumed_after_commit"] == 0
    assert row["ignored_frames_while_responding"] > 0
    assert store.calls_for("SID-RES-OFF") == []


def test_on_cancels_and_answers_both_parts_once(agent, two_mocks):
    start, cfg = agent
    _setup(cfg, resume=True)
    (store, murl), _ = two_mocks
    r = _go(start, "SID-RES-ON", murl)
    assert r["turn_end"]
    assert [e["type"] for e in r["events"]].count("turn.end") == 1
    assert "배송중" in r["text"] and ASK_ID not in r["text"]  # one reply, to the whole utterance
    assert r["first_agent_frame_idx"] > PART2_END_FRAME     # nothing sent before part 2 ended
    rows = [x for x in _metrics(cfg) if x["session_id"] == "SID-RES-ON"]
    assert len(rows) == 1                                   # the cancelled reply wrote no row
    row = rows[0]
    assert row["user_text"] == f"{PART1} {PART2}" and row["resumed_after_commit"] == 1
    assert row["turn"] == 1 and row["outcome"] == "ok"
    assert [c["name"] for c in store.calls_for("SID-RES-ON")] == ["get_order_info"]


def test_on_does_not_cancel_after_a_function_call_was_sent(agent, mock_server):
    """Part 1 already names the order: the lookup is sent during the pause. A lookup is safe to
    send again (the scorer does not count extra lookups; prefetched lookups go out this way all
    the time), so part 2 still cancels the turn and both parts are answered once. Only a
    registration that was sent keeps the turn (tested in test_v3_integration.py)."""
    murl, _ = mock_server
    tools = httpx.get(murl + "/tools").json()
    posts = []

    class SlowTools(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj):
            buf = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(buf)))
            self.end_headers()
            self.wfile.write(buf)

        def do_GET(self):
            self._send(tools)

        def do_POST(self):
            posts.append(self.path)
            time.sleep(1.5)  # still answering when part 2 starts
            self._send({"status": "ok", "data": {"order_id": "DEMO1001", "shipping_status": "배송중"}})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), SlowTools)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    start, cfg = agent
    _setup(cfg, resume=True, llm_delay_ms=50)

    async def go():
        h = await start()
        h["app"].stt = SegmentSTT([PART2, "그리고 하나 더요."])  # part 1 = the order question
        return await run_client(h["url"], "SID-RES-TOOL", PCM, f"http://127.0.0.1:{srv.server_address[1]}",
                                case_id="RESUME", speed=1)

    r = run(go())
    srv.shutdown()
    assert r["turn_end"] and [e["type"] for e in r["events"]].count("turn.end") == 1
    assert "배송중" in r["text"]
    assert posts == ["/tools/get_order_info"] * 2            # the lookup again for the whole utterance
    row = _metrics(cfg)[-1]
    assert row["user_text"] == f"{PART2} 그리고 하나 더요."   # one reply to both parts
    assert row["resumed_after_commit"] == 1 and row["resume_blocked"] is None
