"""End-to-end protocol tests of the agent server with TEST-DOUBLE providers
(FakeSTT mapping by case_id, ScriptedLLM, ToneTTS). They verify transport, endpointing,
tools_url/X-Session-Id, isolation and error handling - not AI quality."""
import asyncio
import json
import threading

import httpx
import pytest
import websockets

from team.agent.config import Config
from team.agent.server import App
from team.agent.session import Session
from team.mock.mock_api import serve

from .helpers import run_client, silence, speechlike

TEXTS = {
    "ORDER_OK": "주문번호 DEMO1001 배송 어디쯤이에요?",
    "ORDER_OK2": "주문번호 데모 이공공이 확인해 주세요.",
    "ORDER_NF": "주문번호 DEMO9999 조회해 주세요.",
    "HANDOFF": "상담원 연결해 주세요.",
    "INCOMPLETE": "어제 주문했는데",
    "EMPTY": "",
}


@pytest.fixture
def two_mocks():
    servers = []
    for _ in range(2):
        srv, store = serve(port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append((srv, store, f"http://127.0.0.1:{srv.server_address[1]}"))
    yield [(s, u) for _, s, u in servers]
    for srv, _, _ in servers:
        srv.shutdown()


@pytest.fixture
def agent(tmp_path):
    mapping = tmp_path / "stt.json"
    mapping.write_text(json.dumps(TEXTS, ensure_ascii=False), encoding="utf-8")
    cfg = Config(port=0, vad="energy", stt_provider="fake", stt_fake_text=str(mapping),
                 llm_provider="scripted", tts_provider="tone", metrics_path=str(tmp_path / "m.jsonl"),
                 tool_timeout_s=2.0)
    holder = {}

    async def start():
        app = App(cfg)
        await app.warm()
        app.http = httpx.AsyncClient()
        srv = await websockets.serve(lambda ws: Session(ws, app).run(), "127.0.0.1", 0, max_size=None)
        holder.update(app=app, srv=srv, url=f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}")
        return holder

    return start, cfg


def run(coro):
    return asyncio.run(coro)


def _metrics(cfg):
    return [json.loads(l) for l in open(cfg.metrics_path, encoding="utf-8")]


def test_order_lookup_turn_end_and_session_header(agent, two_mocks):
    start, cfg = agent
    (store, murl), _ = two_mocks

    async def go():
        h = await start()
        pcm = speechlike(1.5) + silence(0.2)
        return await run_client(h["url"], "SID-1", pcm, murl, case_id="ORDER_OK", speed=4)

    r = run(go())
    assert r["turn_end"] and r["audio_bytes"] > 0
    assert "배송중" in r["text"]
    assert r["msgs"][-1] == ("json", "turn.end")          # turn.end is the last message
    assert r["after_turn_end"] == 0
    assert r["first_agent_frame_idx"] > r["speech_frames"]  # no audio before speech ended
    calls = store.calls_for("SID-1")
    assert [(c["name"], c["arguments"], c["matched"]) for c in calls] == [("get_order_info", {"order_id": "DEMO1001"}, True)]


def test_not_found_is_reported_truthfully(agent, two_mocks):
    start, _ = agent
    (store, murl), _ = two_mocks

    async def go():
        h = await start()
        return await run_client(h["url"], "SID-NF", speechlike(1.2), murl, case_id="ORDER_NF", speed=4)

    r = run(go())
    assert "확인되는 내역이 없습니다" in r["text"] and "배송중" not in r["text"]
    assert store.calls_for("SID-NF")[0]["matched"] is False


def test_tool_server_down_is_not_reported_as_success(agent):
    start, cfg = agent

    async def go():
        h = await start()
        return await run_client(h["url"], "SID-DOWN", speechlike(1.2), "http://127.0.0.1:9", case_id="ORDER_OK", speed=4)

    r = run(go())
    assert r["turn_end"]
    assert "배송중" not in r["text"] and ("확인해 드리지 못했습니다" in r["text"] or "확인하지 못했습니다" in r["text"])
    m = _metrics(cfg)[-1]
    assert m["tools_list_error"]


def test_four_concurrent_sessions_do_not_mix(agent, two_mocks):
    """4 parallel sessions, alternating between two different tools_url servers."""
    start, _ = agent
    (s1, u1), (s2, u2) = two_mocks
    plan = [("C1", "ORDER_OK", u1, "배송중"), ("C2", "ORDER_OK2", u2, "배송완료"),
            ("C3", "ORDER_NF", u1, "확인되는 내역이 없습니다"), ("C4", "ORDER_OK", u2, "배송중")]

    async def go():
        h = await start()
        return await asyncio.gather(*(run_client(h["url"], sid, speechlike(1.0 + 0.3 * i, seed=i), u, case_id=case, speed=2)
                                      for i, (sid, case, u, _) in enumerate(plan)))

    rs = run(go())
    for (sid, case, u, expect), r in zip(plan, rs):
        assert r["turn_end"] and expect in r["text"], (sid, r["text"])
    # each call went to that session's own tools_url with its own X-Session-Id
    assert sorted(c["session_id"] for c in s1.calls_for(None)) == ["C1", "C3"]
    assert sorted(c["session_id"] for c in s2.calls_for(None)) == ["C2", "C4"]
    assert s2.calls_for("C2")[0]["arguments"] == {"order_id": "DEMO2002"}


def test_mid_utterance_pause_with_incomplete_text_does_not_interrupt(agent, two_mocks):
    """1.0 s pause after an unfinished clause: the agent must wait for the second part."""
    start, cfg = agent
    (_, murl), _ = two_mocks
    pcm = speechlike(1.0) + silence(1.0) + speechlike(1.0, seed=3)

    async def go():
        h = await start()
        return await run_client(h["url"], "SID-P", pcm, murl, case_id="INCOMPLETE", speed=4)

    r = run(go())
    assert r["first_agent_frame_idx"] > r["speech_frames"]
    m = _metrics(cfg)[-1]
    assert m["eou_reason"] == "max_silence" and m["resumes"] == 1


def test_complete_looking_text_commits_early_documented_risk(agent, two_mocks):
    """Documents the trade-off: if the first half already *looks* complete, a pause longer
    than complete_silence_ms (1100 ms) is treated as end of turn -> the agent answers in the
    pause. This test asserts the current behaviour so changes are deliberate."""
    start, cfg = agent
    (_, murl), _ = two_mocks
    pcm = speechlike(1.0) + silence(1.3) + speechlike(1.0, seed=3)

    async def go():
        h = await start()
        return await run_client(h["url"], "SID-R", pcm, murl, case_id="HANDOFF", speed=4)

    r = run(go())
    assert r["first_agent_frame_idx"] < r["speech_frames"]  # spoke during the pause
    assert _metrics(cfg)[-1]["eou_reason"] == "complete_text"


def test_silence_only_never_responds_and_empty_stt_asks_again(agent, two_mocks):
    start, _ = agent
    (_, murl), _ = two_mocks

    async def go():
        h = await start()
        quiet = await run_client(h["url"], "SID-Q", silence(0.5), murl, case_id="EMPTY", speed=20, tail_timeout=5)
        empty = await run_client(h["url"], "SID-E", speechlike(1.0), murl, case_id="EMPTY", speed=4)
        return quiet, empty

    quiet, empty = run(go())
    assert quiet.get("timeout") and not quiet["turn_end"] and quiet["audio_bytes"] == 0
    assert empty["turn_end"]
    assert "한 번 더" in empty["text"]


def test_handoff_candidate_does_not_claim_connection(agent, two_mocks):
    start, cfg = agent
    (_, murl), _ = two_mocks

    async def go():
        h = await start()
        return await run_client(h["url"], "SID-H", speechlike(1.0), murl, case_id="HANDOFF", speed=4)

    r = run(go())
    assert "연결해 드릴 수 없습니다" in r["text"] or "완료해 드릴 수 없습니다" in r["text"]
    assert _metrics(cfg)[-1]["handoff_flags"] == ["human_request"]


def test_protocol_errors(agent, two_mocks):
    start, _ = agent
    (_, murl), _ = two_mocks

    async def go():
        h = await start()
        a = await run_client(h["url"], "X", b"", murl, first_message=b"\0" * 640, tail_timeout=2, speed=10)
        b = await run_client(h["url"], "X", b"", murl, first_message=json.dumps(
            {"type": "session.start", "session_id": "X", "audio": {"encoding": "opus"}}), tail_timeout=2, speed=10)
        return a, b

    a, b = run(go())
    assert a.get("closed") == 1002 and b.get("closed") == 1002


def test_complete_text_with_sentence_gap_waits(agent, two_mocks):
    """A 0.9 s gap between two sentences (measured SAPI inter-sentence gap) must not end the turn."""
    start, cfg = agent
    (_, murl), _ = two_mocks
    pcm = speechlike(1.0) + silence(0.9) + speechlike(1.0, seed=4)

    async def go():
        h = await start()
        return await run_client(h["url"], "SID-G", pcm, murl, case_id="HANDOFF", speed=4)

    r = run(go())
    assert r["first_agent_frame_idx"] > r["speech_frames"]


def test_slow_tool_server_hits_turn_budget_and_still_ends_turn(agent):
    """Function server that hangs: the agent must speak a fallback and send turn.end within
    turn_budget_s (evaluator disconnects 25 s after speech end)."""
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from team.mock.mock_api import DEFAULT_TOOLS

    tools = json.loads(open(DEFAULT_TOOLS, encoding="utf-8").read())

    class Slow(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            buf = json.dumps([{"type": "function", "function": t} for t in tools]).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(buf))); self.end_headers()
            self.wfile.write(buf)

        def do_POST(self):
            time.sleep(4)
            self.send_response(200); self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    start, cfg = agent
    cfg.turn_budget_s, cfg.tool_timeout_s = 1.5, 10.0

    async def go():
        h = await start()
        t = time.perf_counter()
        r = await run_client(h["url"], "SID-SLOW", speechlike(1.0), f"http://127.0.0.1:{srv.server_address[1]}",
                             case_id="ORDER_OK", speed=1)
        return r, time.perf_counter() - t

    r, elapsed = run(go())
    srv.shutdown()
    assert r["turn_end"] and "지연" in r["text"] and "배송중" not in r["text"]
    assert elapsed < 1.0 + 1.8 + 1.5 + 1.5   # speech + max silence + budget + slack
    assert _metrics(cfg)[-1]["outcome"] == "budget_timeout"


def test_scripted_double_order_id_parsing():
    from team.agent.providers.llm import ScriptedLLM
    f = ScriptedLLM._order_id
    assert f("주문 번호는 데모 1001이에요.") == "DEMO1001"
    assert f("주문번호 데모 1001 아 아니다 데모 2002 배송상태") == "DEMO2002"
    assert f("DEMO-9999 조회") == "DEMO9999"
    assert f("배송 문의요") is None


def test_odd_sized_frames_are_reassembled(agent, two_mocks):
    from team.agent.audio import FrameAssembler
    fa = FrameAssembler(640)
    frames = list(fa.push(b"\1" * 1001)) + list(fa.push(b"\1" * 279))
    assert [len(f) for f in frames] == [640, 640]
