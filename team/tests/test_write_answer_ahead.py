"""write_answer_ahead (dialog.py): when one registration passes the gate, the reply is written while
the call runs and is said only after the real result is ok; any other result throws it away and the
real result is answered as before. Made-up values, fake LLM and tools, no network."""
import asyncio
import json
import time

from team.agent import policy
from team.agent.config import Config
from team.agent.dialog import Dialog
from team.agent.gate import Gate
from team.agent.providers.llm import ToolCall

ORD = "20990101-0001234"
HIST = [{"role": "customer", "text": f"주문번호 {ORD} 반품하고 싶어요"},
        {"role": "agent", "text": "전체 반품으로 접수해 드릴까요?"}]
RETURN = ToolCall("c0", "return_order", json.dumps({"order_number": ORD}))


class LLM:
    """Round 1: the registration. Later rounds: what the model would say for the result it sees."""

    def __init__(self, delay=0.2):
        self.delay, self.calls = delay, []

    async def stream(self, messages, tools=None, tool_choice="auto"):
        self.calls.append((time.perf_counter(), [dict(m) for m in messages]))
        if len(self.calls) == 1:
            yield ("tool_calls", [ToolCall(RETURN.id, RETURN.name, RETURN.arguments)])
            return
        await asyncio.sleep(self.delay)
        status = json.loads(messages[-1]["content"]).get("status")
        yield ("text", "반품 접수되었습니다. " if status == "ok" else "이미 반품이 접수된 주문입니다. ")


class Tools:
    def __init__(self, result, delay=0.3):
        self.result, self.delay, self.sent, self.t_result = result, delay, [], None

    def openai_schemas(self):
        return [{"type": "function", "function": {"name": "return_order", "parameters": {"type": "object", "properties": {}}}}]

    async def call(self, name, arguments_json):
        self.sent.append((time.perf_counter(), name))
        await asyncio.sleep(self.delay)
        self.t_result = time.perf_counter()
        return dict(self.result)


def turn(result, on=True):
    cfg = Config(write_answer_ahead=on, prefetch=False)
    llm, tools = LLM(), Tools(result)
    dlg = Dialog(llm, tools, "지침", cfg, {"tool_round_limit": "x"},
                 gate=Gate(policy.load_rules(cfg.rules_path), {"history": HIST}))

    async def go():
        m, out = {}, []
        async for s in dlg.run_turn("네, 전체로 해 주세요", m):
            out.append((s, time.perf_counter()))
        return m, out
    m, out = asyncio.run(go())
    return llm, tools, dlg, m, out


def test_ok_reply_is_written_during_the_call_and_said_after_the_result():
    llm, tools, dlg, m, out = turn({"status": "ok", "message": "주문 반품 접수 성공"})
    assert m["write_answer_ahead"] == "used"
    assert [n for _, n in tools.sent] == ["return_order"]               # sent once
    assert llm.calls[1][0] - tools.sent[0][0] < 0.1                     # reply started with the call
    assert out == [(out[0][0], out[0][1])] and out[0][0] == "반품 접수되었습니다."
    assert out[0][1] >= tools.t_result                                   # said after the real result
    tool_msgs = [x for x in dlg.messages if x["role"] == "tool"]
    assert json.loads(tool_msgs[-1]["content"])["message"] == "주문 반품 접수 성공"  # history: the real result
    assert len(llm.calls) == 2


def test_other_result_throws_the_prepared_reply_away():
    llm, tools, dlg, m, out = turn({"status": "already_requested", "message": "이미 접수됨"})
    assert m["write_answer_ahead"] == "discarded"
    assert [s for s, _ in out] == ["이미 반품이 접수된 주문입니다."]
    assert len(llm.calls) == 3                                           # one more round with the real result


def test_off_waits_for_the_result_before_writing_the_reply():
    llm, tools, dlg, m, out = turn({"status": "ok"}, on=False)
    assert "write_answer_ahead" not in m
    assert llm.calls[1][0] >= tools.t_result
    assert [s for s, _ in out] == ["반품 접수되었습니다."]


def test_a_blocked_registration_is_not_prepared():
    cfg = Config(prefetch=False)
    llm, tools = LLM(), Tools({"status": "ok"})
    dlg = Dialog(llm, tools, "지침", cfg, {"tool_round_limit": "x"},
                 gate=Gate(policy.load_rules(cfg.rules_path), {"history": HIST}))

    async def go():
        m = {}
        return m, [s async for s in dlg.run_turn("잠깐만요, 생각 좀 해 볼게요", m)]
    m, out = asyncio.run(go())
    assert tools.sent == [] and "write_answer_ahead" not in m
