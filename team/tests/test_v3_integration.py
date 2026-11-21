"""v3 pieces working together (no network, no keys):
- resume_before_audio stops listening only after a REGISTRATION was sent (lookups, also prefetched
  ones, are safe to send again)
- a rolled-back turn gets its prefetched lookup results again
- LLM warm-up: one small call with the real system message when the LLM was idle, none while calls
  keep coming"""
import asyncio
import time
from types import SimpleNamespace

from team.agent.config import Config
from team.agent.prefetch import Prefetch
from team.agent.session import Session
from team.agent.tools_client import ToolsClient


class Resp:
    status_code, text = 200, ""

    def json(self):
        return {"status": "ok"}


class HTTP:
    async def post(self, url, json=None, timeout=None, headers=None):
        return Resp()


def test_tools_client_counts_registrations_apart_from_lookups():
    tc = ToolsClient(HTTP(), "http://tools", "SID", 1.0)
    tc.tools = [{"name": n, "parameters": {"type": "object", "properties": {}}}
                for n in ("get_order_info", "get_return_info", "search_products", "return_order")]

    async def go():
        for n in ("get_order_info", "get_return_info", "search_products", "return_order"):
            await tc.call(n, "{}")
    asyncio.run(go())
    assert tc.sent_count == 4 and tc.sent_writes == 1


def listening_session(sent_writes):
    s = object.__new__(Session)
    s.m = {"_writes_sent_at_commit": 0}
    s.resp_task = SimpleNamespace(done=lambda: False)
    s.tools = SimpleNamespace(sent_count=3, sent_writes=sent_writes)
    s.resp_heard = bytearray()
    s.ep = SimpleNamespace(feed=lambda frame: "silence")
    s.stopped = False

    def stop():
        s.stopped = True
    s._stop_listening = stop
    return s


def test_only_a_sent_registration_stops_listening_before_audio():
    lookups_only = listening_session(sent_writes=0)
    assert lookups_only._listen_before_audio(b"\0\0", 0.0) is True and not lookups_only.stopped
    after_write = listening_session(sent_writes=1)
    assert after_write._listen_before_audio(b"\0\0", 0.0) is False
    assert after_write.stopped and after_write.m["resume_blocked"] == "write_sent"


class Tools:
    async def call(self, name, arguments_json):
        return {"status": "ok", "data": {}}


def test_a_rolled_back_turn_gets_the_prefetched_results_again():
    async def go():
        pf = Prefetch(Tools())
        pf.turn_start("제 번호 010-9999-0000이에요")
        await pf.settle(1)
        first = pf.ready(5)
        again_before = pf.ready(5)
        pf.forget_given(5)            # the turn that started at message 5 was rolled back
        again_after = pf.ready(5)
        return first, again_before, again_after
    first, before, after = asyncio.run(go())
    assert len(first) == 2 and before == [] and len(after) == 2


class LLM:
    def __init__(self):
        self.calls = []

    async def stream(self, messages, tools=None, tool_choice="auto"):
        self.calls.append((messages, tools, tool_choice))
        yield ("text", "네.")


def warm_session(app, idle_s=240.0):
    s = object.__new__(Session)
    s.app, s.sid, s.debug = app, "SID", None
    s.cfg = Config(llm_warm_idle_s=idle_s)
    s.dialog = SimpleNamespace(messages=[{"role": "system", "content": "지침 + 브랜드 자료"}])
    s.tools = SimpleNamespace(openai_schemas=lambda: [{"type": "function", "function": {"name": "get_order_info"}}])
    return s


def test_warm_up_once_when_idle_and_not_while_calls_keep_coming():
    app = SimpleNamespace(llm=LLM())

    async def go():
        done = asyncio.get_running_loop().create_future()
        done.set_result([])
        await warm_session(app)._warm_llm(done)      # idle (never used): warms up
        await warm_session(app)._warm_llm(done)      # just used: nothing sent
        app.llm_last_used = time.monotonic() - 300   # idle again
        await warm_session(app)._warm_llm(done)
    asyncio.run(go())
    assert len(app.llm.calls) == 2
    msgs, tools, choice = app.llm.calls[0]
    assert msgs[0]["content"] == "지침 + 브랜드 자료" and tools and choice == "none"  # same prefix as real calls
