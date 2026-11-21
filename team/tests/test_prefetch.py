"""Prefetch (team/agent/prefetch.py): order / return lookups called ahead for real, never registrations;
finished results go to the model before round 1, a running one serves the model's own call, and every
prefetch is finished before the first answer sentence. Made-up values, fake tools and LLM, no network."""
import asyncio
import json
import time

from team.agent.config import Config
from team.agent.dialog import Dialog
from team.agent.prefetch import Prefetch, key, lookups_for, numbers
from team.agent.providers.llm import ToolCall

PH, ORD = "010-9999-0000", "20990101-0001234"
CALLER = {"name": "홍길동", "phone": PH, "email": None}
ORDER_TALK = [{"role": "customer", "text": "주문한 거 배송이 안 와요"}, {"role": "agent", "text": "확인해 보겠습니다."}]


class Tools:
    """tools_url stand-in: every lookup takes `delay` seconds; records what was sent."""

    def __init__(self, delay=0.05):
        self.delay, self.sent, self.done_at = delay, [], {}

    def openai_schemas(self):
        return [{"type": "function", "function": {"name": n, "parameters": {"type": "object", "properties": {}}}}
                for n in ("get_order_info", "get_return_info", "cancel_order")]

    async def call(self, name, arguments_json):
        self.sent.append((name, json.loads(arguments_json)))
        await asyncio.sleep(self.delay)
        self.done_at[name] = time.perf_counter()
        return {"status": "ok", "data": {"orders": [{"주문번호": ORD, "배송상태": "배송중"}]}}


def test_numbers_map_to_lookups_only():
    assert numbers(f"{PH} 주문번호 {ORD} 2099-01-05 12,000원") == [PH, ORD]
    assert lookups_for(PH) == [("get_order_info", {"buyer_cellphone": PH}), ("get_return_info", {"customer_phone_number": PH})]
    assert lookups_for(ORD) == [("get_order_info", {"order_id": ORD})]
    assert lookups_for("20990101000123401") == []  # item code: no lookup takes it
    assert key("get_order_info", {"order_id": ORD}) == key("get_order_info", {"order_id": "209901010001234"})


def run(coro):
    return asyncio.run(coro)


def test_session_start_only_when_the_talk_is_about_orders_and_not_twice():
    async def go():
        t = Tools()
        pf = Prefetch(t, history=ORDER_TALK, caller=CALLER)
        pf.session_start()
        pf.turn_start(f"제 번호 {PH}이에요")  # the same lookups again: not sent twice
        await pf.settle(1)
        t2 = Tools()
        Prefetch(t2, history=[{"role": "customer", "text": "비타민 성분 궁금해요"}], caller=CALLER).session_start()
        await asyncio.sleep(0.1)
        done = [{"role": "tool_call", "name": "get_order_info", "arguments": {"buyer_cellphone": "01099990000"}}]
        t3 = Tools()
        pf3 = Prefetch(t3, history=ORDER_TALK + done, caller=CALLER)
        pf3.session_start()
        await pf3.settle(1)
        return t.sent, t2.sent, t3.sent
    sent, sent2, sent3 = run(go())
    assert sorted(n for n, _ in sent) == ["get_order_info", "get_return_info"]
    assert sent2 == []                                   # product question: caller phone not looked up
    assert [n for n, _ in sent3] == ["get_return_info"]  # the order lookup is already in the history


class OneRoundLLM:
    def __init__(self, rounds):
        self.rounds, self.seen = rounds, []

    async def stream(self, messages, tools=None, tool_choice="auto"):
        self.seen.append([dict(m) for m in messages])
        for item in self.rounds.pop(0):
            yield item


def turn(dlg, text):
    async def go():
        m, out = {}, []
        async for s in dlg.run_turn(text, m):
            out.append((s, time.perf_counter()))
        return m, out
    return go()


def test_finished_prefetch_goes_in_before_round_one_and_the_model_answers_once():
    async def go():
        t = Tools()
        llm = OneRoundLLM([[("text", "주문하신 상품은 배송중입니다. ")]])
        dlg = Dialog(llm, t, "지침", Config(), {"tool_round_limit": "x"}, context="[상담 정보]")
        dlg.prefetch = Prefetch(t, history=ORDER_TALK, caller=CALLER)
        dlg.prefetch.session_start()
        await dlg.prefetch.settle(1)          # finished while the customer was speaking
        m, out = await turn(dlg, "언제 와요?")
        return llm, m, out
    llm, m, out = run(go())
    first = llm.seen[0]
    assert [x["role"] for x in first[-3:]] == ["assistant", "tool", "tool"]  # calls + results before round 1
    assert sorted(m["prefetch_given"]) == ["get_order_info", "get_return_info"]
    assert len(llm.seen) == 1 and out[0][0] == "주문하신 상품은 배송중입니다."


def test_running_prefetch_serves_the_models_own_lookup_and_settles_before_the_first_sentence():
    async def go():
        t = Tools(delay=0.3)
        rounds = [[("tool_calls", [ToolCall("c0", "get_order_info", json.dumps({"order_id": "209901010001234"}))])],
                  [("text", "배송중입니다. ")]]
        llm = OneRoundLLM(rounds)
        dlg = Dialog(llm, t, "지침", Config(), {"tool_round_limit": "x"})
        dlg.prefetch = Prefetch(t)
        m, out = await turn(dlg, f"주문번호 {ORD} 배송 언제 와요?")
        return t, m, out
    t, m, out = run(go())
    assert t.sent == [("get_order_info", {"order_id": ORD})]   # sent once: the model's call was served
    assert out[0][1] >= t.done_at["get_order_info"]            # spoken after the last function result
    assert "prefetch_given" not in m


def test_text_answer_waits_for_a_running_prefetch_and_registrations_are_never_prefetched():
    async def go():
        t = Tools(delay=0.3)
        llm = OneRoundLLM([[("text", "네, 말씀하세요. ")]])
        dlg = Dialog(llm, t, "지침", Config(), {"tool_round_limit": "x"})
        dlg.prefetch = Prefetch(t)
        m, out = await turn(dlg, f"{PH} 주문 취소해 주세요")
        return t, m, out
    t, m, out = run(go())
    assert {n for n, _ in t.sent} == {"get_order_info", "get_return_info"}  # never cancel_order
    assert out[0][1] >= max(t.done_at.values())
    assert m.get("prefetch_wait_ms", 0) > 0


def test_off_by_config():
    assert Config(prefetch=False).prefetch is False and Config().prefetch is True
