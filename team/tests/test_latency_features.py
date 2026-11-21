"""Latency features: early first chunk (comma split) and tool_ack (pre-synthesized phrase while
functions run). Test doubles only (FakeSTT, ScriptedLLM, ToneTTS) - says nothing about AI quality."""
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from team.agent.config import Config
from team.agent.dialog import Ack, Dialog
from team.agent.providers.llm import ScriptedLLM
from team.agent.textutil import SentenceSplitter
from team.agent.tools_client import ToolsClient

from .helpers import run_client, silence, speechlike
from .test_agent_protocol import _metrics, agent, run, two_mocks  # noqa: F401  (fixtures)

ACKS = ["확인해 보겠습니다.", "잠시만요, 조회해 볼게요.", "네, 바로 찾아보겠습니다."]


def test_first_chunk_may_end_at_comma():
    s = SentenceSplitter(early_min_chars=8)
    out = s.push("주문하신 상품 확인했고요, 지금 ")
    assert out == ["주문하신 상품 확인했고요,"]
    out += s.push("배송중이고, 내일 도착 예정입니다. ")  # later chunks: sentences only
    out += s.flush()
    assert out == ["주문하신 상품 확인했고요,", "지금 배송중이고, 내일 도착 예정입니다."]


def test_first_chunk_comma_too_early_or_sentence_first_is_ignored():
    s = SentenceSplitter(early_min_chars=8)
    assert s.push("네 고객님, ") == []             # comma before 8 chars: wait
    s2 = SentenceSplitter(early_min_chars=8)
    assert s2.push("배송중으로 확인됩니다. 그리고, 내일 도착해요") == ["배송중으로 확인됩니다."]
    assert SentenceSplitter().push("주문하신 상품 확인했고요, 지금 ") == []  # default: off


def test_ack_is_off_by_default():
    assert Config().tool_ack is False and Config().first_chunk_min_chars == 0


def test_order_lookup_plays_cached_ack_first(agent, two_mocks):
    start, cfg = agent
    cfg.tool_ack = True
    cfg.topic_ack = False   # the sample is a delivery question: the topic line would replace tool_ack
    (store, murl), _ = two_mocks

    async def go():
        h = await start()
        assert set(h["app"].cached_text) == set(ACKS)   # pre-synthesized at startup
        return await run_client(h["url"], "SID-ACK", speechlike(1.5) + silence(0.2), murl, case_id="ORDER_OK", speed=4)

    r = run(go())
    texts = [e["text"] for e in r["events"] if e.get("type") == "agent.transcript"]
    assert texts[0] in ACKS and "배송중" in " ".join(texts[1:])
    row = _metrics(cfg)[-1]
    assert row["tts_ms"][0] == 0.0 and row["outcome"] == "ok"


def test_ack_rotates_and_can_be_disabled(mock_server):
    murl, _ = mock_server

    async def turns(cfg):
        async with httpx.AsyncClient() as http:
            tools = ToolsClient(http, murl, "SID-ROT", 2.0)
            await tools.load()
            d = Dialog(ScriptedLLM(cfg), tools, "system", cfg,
                       {"tool_round_limit": "x", "tool_ack": ACKS})
            out = []
            for _ in range(2):
                out.append([s async for s in d.run_turn("주문번호 DEMO1001 배송 어디예요?", {})])
            return out

    t1, t2 = asyncio.run(turns(Config(scripted_delay_ms=0, tool_ack=True)))
    assert isinstance(t1[0], Ack) and isinstance(t2[0], Ack) and t1[0] != t2[0]
    assert "배송중" in t1[1]
    off = asyncio.run(turns(Config(scripted_delay_ms=0, tool_ack=False)))
    assert not any(isinstance(s, Ack) for turn in off for s in turn)


def test_ack_does_not_suppress_timeout_fallback(agent, mock_server):
    murl, _ = mock_server
    tools = httpx.get(murl + "/tools").json()

    class Slow(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            buf = json.dumps(tools).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(buf))); self.end_headers()
            self.wfile.write(buf)

        def do_POST(self):
            time.sleep(4)
            self.send_response(200); self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    start, cfg = agent
    cfg.turn_budget_s, cfg.tool_timeout_s, cfg.tool_ack = 1.5, 10.0, True
    cfg.topic_ack = False   # this test is about tool_ack (test_topic_ack.py covers the topic line)

    async def go():
        h = await start()
        return await run_client(h["url"], "SID-SLOW-ACK", speechlike(1.0),
                                f"http://127.0.0.1:{srv.server_address[1]}", case_id="ORDER_OK", speed=1)

    r = run(go())
    srv.shutdown()
    texts = [e["text"] for e in r["events"] if e.get("type") == "agent.transcript"]
    assert texts[0] in ACKS                     # said right away
    assert any("지연" in t for t in texts[1:])   # and the delay fallback still follows
    assert r["turn_end"] and _metrics(cfg)[-1]["outcome"] == "budget_timeout"
