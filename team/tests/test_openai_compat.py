"""Wire-format test of OpenAICompatLLM + Dialog against a local fake chat-completions SSE
endpoint (OpenAI streaming format: content deltas, tool_call deltas split across chunks).
Verifies our parsing / tool loop code without any API key. Says nothing about model quality."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from team.agent.config import Config
from team.agent.dialog import Dialog
from team.agent.providers.llm import OpenAICompatLLM
from team.agent.tools_client import ToolsClient

REQUESTS = []


def _chunk(delta, finish=None):
    return {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "fake",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class FakeLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        REQUESTS.append({"auth": self.headers.get("Authorization"), "body": body})
        last = body["messages"][-1]
        if last["role"] == "user":  # round 1: ask for a tool, arguments split over chunks
            chunks = [_chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                                    "function": {"name": "get_order_info", "arguments": ""}}]}),
                      _chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{\"order_"}}]}),
                      _chunk({"tool_calls": [{"index": 0, "function": {"arguments": "id\": \"DEMO1001\"}"}}]}),
                      _chunk({}, "tool_calls")]
        else:  # round 2: answer using the tool result that was sent back
            status = json.loads(last["content"])["data"]["shipping_status"]
            chunks = [_chunk({"role": "assistant", "content": "조회 결과 "}),
                      _chunk({"content": f"{status} 상태입니다. 다른"}),
                      _chunk({"content": " 문의 있으신가요?"}), _chunk({}, "stop")]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")


def test_openai_compat_stream_with_tool_call(mock_server, monkeypatch):
    murl, store = mock_server
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("TEST_FAKE_LLM_KEY", "sk-test-not-real")
    cfg = Config(llm_base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1", llm_model="fake",
                 llm_api_key_env="TEST_FAKE_LLM_KEY")

    async def go():
        async with httpx.AsyncClient() as http:
            tools = ToolsClient(http, murl, "SID-OAI", 2.0)
            await tools.load()
            d = Dialog(OpenAICompatLLM(cfg), tools, "system", cfg, {"tool_round_limit": "x"})
            m = {}
            return [s async for s in d.run_turn("주문번호 DEMO1001 배송 어디예요?", m)], m, d.messages

    sents, m, msgs = asyncio.run(go())
    srv.shutdown()
    assert sents == ["조회 결과 배송중 상태입니다.", "다른 문의 있으신가요?"]
    assert store.calls_for("SID-OAI")[0]["arguments"] == {"order_id": "DEMO1001"}
    assert REQUESTS[0]["auth"] == "Bearer sk-test-not-real"
    assert [t["function"]["name"] for t in REQUESTS[0]["body"]["tools"]] == ["get_order_info", "request_callback"]
    assert REQUESTS[0]["body"]["stream"] is True
    assert [x["role"] for x in msgs] == ["system", "user", "assistant", "tool", "assistant"]
    assert m["llm_rounds"] == 2 and m["tool_calls"][0]["calls"] == [{"name": "get_order_info", "status": "ok"}]


def test_missing_key_fails_fast(monkeypatch):
    monkeypatch.delenv("NO_SUCH_KEY_ENV", raising=False)
    cfg = Config(llm_api_key_env="NO_SUCH_KEY_ENV", llm_base_url="https://api.example.com/v1")
    try:
        OpenAICompatLLM(cfg)
        raise AssertionError("expected LLMConfigError")
    except Exception as e:
        assert "NO_SUCH_KEY_ENV" in str(e) and "sk-" not in str(e)
