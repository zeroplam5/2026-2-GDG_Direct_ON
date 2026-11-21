"""Number slots (team/agent/number_slots.py): the KULLM gateway masks phone / order numbers in
user messages, so they go to the model as slot names with a table in the system message, and slot
names the model writes come back as real values. Made-up values, fake LLM, no network."""
import asyncio
import json

from team.agent import policy
from team.agent.config import Config
from team.agent.dialog import Dialog
from team.agent.gate import Gate
from team.agent.number_slots import NumberSlots
from team.agent.providers.llm import ToolCall

PH, PH2, ORD, ITEM = "010-9999-0000", "010-9999-0001", "20990101-0001234", "20990101000123401"


def test_ids_get_slots_dates_prices_and_short_numbers_do_not():
    s = NumberSlots()
    text = (f"{PH} / 01099990000 / 010 9999 0000 / +82 10-9999-0000 / 02-123-4567 / 주문번호 {ORD} / "
            f"상세 {ITEM} / 2099-01-05 / 2026-11-21T14:00:00 / 12,000원 / 12000원 / 1588-1234 / 우편 06236")
    enc = s.encode(text)
    for v in (PH, "01099990000", "010 9999 0000", "+82 10-9999-0000", "02-123-4567", ORD, ITEM):
        assert v not in enc
    for v in ("2099-01-05", "2026-11-21T14:00:00", "12,000원", "12000원", "1588-1234", "06236"):
        assert v in enc
    assert s.decode(enc) == text


def test_a_date_or_price_after_a_number_is_not_swallowed():
    s = NumberSlots()
    assert s.encode(f"주문번호 {ORD} 2099-01-05에 12000원 결제") == "주문번호 [번호1] 2099-01-05에 12000원 결제"
    assert s.encode(f"{ORD} {PH}") == "[번호1] [번호2]"


def test_same_number_same_slot_and_unknown_slots_stay():
    s = NumberSlots()
    assert s.encode(f"{PH} 아니 {PH2}, 다시 {PH}") == "[번호1] 아니 [번호2], 다시 [번호1]"
    assert s.decode("[번호2]로 연락드릴게요 [번호9]") == f"{PH2}로 연락드릴게요 [번호9]"
    assert NumberSlots().table() == ""


def test_outgoing_only_user_messages_and_the_table_at_the_end_of_the_system_message():
    s = NumberSlots()
    msgs = [{"role": "system", "content": f"지침\n\n발신자 {PH}"},
            {"role": "user", "content": f"{PH} 홍길동이에요"},
            {"role": "assistant", "content": f"{PH}로 확인됩니다."},
            {"role": "tool", "tool_call_id": "c", "content": json.dumps({"주문번호": ORD}, ensure_ascii=False)}]
    before = json.dumps(msgs, ensure_ascii=False)
    out = s.outgoing(msgs)
    assert json.dumps(msgs, ensure_ascii=False) == before            # the dialog history is not changed
    assert out[1]["content"] == "[번호1] 홍길동이에요"
    assert out[2] is msgs[2] and out[3] is msgs[3]                    # assistant / tool pass unchanged
    assert out[0]["content"].startswith(f"지침\n\n발신자 {PH}")         # system: static part first (cache)
    assert out[0]["content"].endswith(f"[번호1] = {PH}")


class SlotLLM:
    """Answers with slot names, like the model does after reading the table."""

    def __init__(self):
        self.seen = []
        self.rounds = [[("tool_calls", [ToolCall("c0", "request_callback",
                                                 json.dumps({"phone_number": "[번호2]", "customer_name": "홍길동"}, ensure_ascii=False))])],
                       [("text", "[번호2]로 전화 요청을 접수했습니다. ")]]

    async def stream(self, messages, tools=None, tool_choice="auto"):
        self.seen.append([dict(m) for m in messages])
        for item in self.rounds.pop(0):
            yield item


class Tools:
    def __init__(self):
        self.sent = []

    def openai_schemas(self):
        return [{"type": "function", "function": {"name": "request_callback", "parameters": {"type": "object", "properties": {}}}}]

    async def call(self, name, arguments_json):
        self.sent.append((name, json.loads(arguments_json)))
        return {"status": "ok"}


def _turn(dlg, text):
    async def go():
        return [s async for s in dlg.run_turn(text, {})]
    return asyncio.run(go())


def test_dialog_sends_slots_and_the_gate_and_tools_get_real_values():
    cfg = Config()
    rules = policy.load_rules(cfg.rules_path)
    llm, tools = SlotLLM(), Tools()
    dlg = Dialog(llm, tools, "지침", cfg, {"tool_round_limit": "x"},
                 gate=Gate(rules, {"history": [{"role": "agent", "text": "연락받으실 번호와 성함을 알려 주세요."}]}),
                 context="[상담 정보]")
    said = _turn(dlg, f"{PH}이요. 아 잘못 말했다, {PH2}이에요. 홍길동이고요. 전화 부탁드려요")
    user_sent = [m for m in llm.seen[0] if m["role"] == "user"][0]["content"]
    assert PH not in user_sent and PH2 not in user_sent and "[번호2]" in user_sent
    assert llm.seen[0][0]["content"].endswith(f"[번호1] = {PH}\n[번호2] = {PH2}")
    assert tools.sent == [("request_callback", {"phone_number": PH2, "customer_name": "홍길동"})]  # gate passed it
    assert said == [f"{PH2}로 전화 요청을 접수했습니다."]
    assert PH2 in dlg.messages[1]["content"] and "[번호" not in json.dumps(dlg.messages, ensure_ascii=False)


def test_off_sends_messages_unchanged():
    cfg = Config(number_slots=False)
    llm = SlotLLM()
    llm.rounds = [[("text", "네. ")]]
    dlg = Dialog(llm, Tools(), "지침", cfg, {"tool_round_limit": "x"})
    _turn(dlg, f"{PH} 홍길동이에요")
    assert [m for m in llm.seen[0] if m["role"] == "user"][0]["content"] == f"{PH} 홍길동이에요"
    assert "[번호 자리표]" not in llm.seen[0][0]["content"]
