"""Structure around the LLM loop:
S6 a question already said in a blocked round is not repeated,
S3 a blocked call with a confirm_template is answered without a second model call,
S2 every model sentence passes the output check,
S4 static instructions come first in the system message, per-call context last (cache)."""
import asyncio
import json

from team.agent import policy
from team.agent.config import Config
from team.agent.dialog import Dialog
from team.agent.gate import CONFIRM_FIELD, Gate
from team.agent.output_guard import OutputGuard, numbers_in
from team.agent.providers.llm import ToolCall

CFG = Config()
RULES = policy.load_rules(CFG.rules_path)
CALLER = {"name": "연습 고객", "phone": "01000001234", "email": None}
TOOLS = [{"type": "function", "function": {"name": "request_callback", "description": "",
          "parameters": {"type": "object", "required": ["phone_number"],
                         "properties": {"phone_number": {"type": "string"}}}}}]
PHRASES = {"tool_round_limit": "x"}


class ScriptLLM:
    """Plays a fixed list of rounds; fails the test if asked for more rounds than given."""

    def __init__(self, *rounds):
        self.rounds = list(rounds)
        self.calls = 0
        self.seen = []

    async def stream(self, messages, tools=None, tool_choice="auto"):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        assert self.rounds, "model was called more times than expected"
        for item in self.rounds.pop(0):
            yield item


class FakeTools:
    def __init__(self):
        self.sent = []

    def openai_schemas(self):
        return TOOLS

    async def call(self, name, arguments_json):
        self.sent.append((name, arguments_json))
        return {"status": "ok"}


def callback(quote=""):
    return ToolCall("c0", "request_callback", json.dumps({"phone_number": "01000001234", CONFIRM_FIELD: quote}))


def run(dlg, text):
    m = {}

    async def go():
        return [str(s) async for s in dlg.run_turn(text, m)]

    return asyncio.run(go()), m


def test_s6_question_said_in_the_blocked_round_is_not_repeated():
    llm = ScriptLLM([("text", "지금 거신 번호로 내일 연락드려도 될까요? "), ("tool_calls", [callback()])])
    tools = FakeTools()
    dlg = Dialog(llm, tools, "system", CFG, PHRASES, gate=Gate(RULES, {"caller": CALLER}))
    said, m = run(dlg, "내일 오전에 다시 전화 주실 수 있나요?")
    assert tools.sent == []
    assert llm.calls == 1 and len(said) == 1  # said once, no second round
    assert m["blocked_reply"] == "already_asked"


def test_s3_template_answers_a_blocked_call_without_a_second_model_call():
    llm = ScriptLLM([("tool_calls", [callback()])])
    dlg = Dialog(llm, FakeTools(), "system", CFG, PHRASES, gate=Gate(RULES, {"caller": CALLER}))
    said, m = run(dlg, "내일 오전에 다시 전화 주실 수 있나요?")
    assert llm.calls == 1
    assert said == ["01000001234 번호로 상담원 연락을 접수해 드릴까요?"]
    assert m["blocked_reply"] == "template"


def test_s3_filler_without_a_question_does_not_end_the_turn():
    llm = ScriptLLM([("text", "확인해 볼게요. "), ("tool_calls", [callback()])])
    dlg = Dialog(llm, FakeTools(), "system", CFG, PHRASES, gate=Gate(RULES, {"caller": CALLER}))
    said, m = run(dlg, "내일 오전에 다시 전화 주실 수 있나요?")
    assert said[-1].endswith("드릴까요?") and m["blocked_reply"] == "template"


def test_s2_output_check_flags_numbers_found_nowhere():
    assert numbers_in("반품은 14일 이내, 배송비 3,000원") == ["14", "3000"]
    g = OutputGuard("log")
    out, flag = g.check("반품은 14일 이내 가능합니다.", "매뉴얼: 반품은 7일 이내")
    assert out == "반품은 14일 이내 가능합니다." and flag["values"] == ["14"]
    assert g.check("주문번호 DEMO1001 건은 배송중입니다.", '{"order_id": "DEMO1001"}')[1] is None
    out, flag = OutputGuard("block").check("반품은 14일 이내 가능합니다.", "매뉴얼: 반품은 7일 이내")
    assert out != "반품은 14일 이내 가능합니다." and flag["action"] == "block"


def test_s2_dialog_records_output_flags():
    llm = ScriptLLM([("text", "반품은 14일 이내에 가능합니다.")])
    dlg = Dialog(llm, FakeTools(), "system 반품은 7일 이내", CFG, PHRASES)
    said, m = run(dlg, "반품 기간이 어떻게 돼요?")
    assert said == ["반품은 14일 이내에 가능합니다."]  # default mode "log" keeps the sentence
    assert m["output_flags"][0]["values"] == ["14"]


def test_s2_a_sentence_already_said_this_turn_is_dropped():
    # gpt-5.4-mini via OpenRouter sometimes writes its whole answer twice
    llm = ScriptLLM([("text", "그 번호로 접수해 드릴까요? 동의하시면 말씀해 주세요.\n\n"
                              "그 번호로 접수해 드릴까요? 동의하시면 말씀해 주세요.")])
    dlg = Dialog(llm, FakeTools(), "system", CFG, PHRASES)
    said, m = run(dlg, "콜백 부탁드려요")
    assert said == ["그 번호로 접수해 드릴까요?", "동의하시면 말씀해 주세요."]
    assert [f["kind"] for f in m["output_flags"]] == ["repeated_sentence", "repeated_sentence"]
    assert dlg.messages[-1]["content"] == "그 번호로 접수해 드릴까요? 동의하시면 말씀해 주세요."  # what was heard


def test_s4_static_prompt_has_no_per_call_values_and_context_comes_second():
    template = policy.load_prompt(CFG.prompt_path)
    phrases = policy.load_phrases(CFG.phrases_path)
    start = {"now": "2026-11-21 11:00", "caller": CALLER, "history": [{"role": "agent", "text": "안녕하세요"}]}
    static = policy.build_static_prompt(template, phrases, RULES, start["now"])
    assert "2026-11-21" not in static and "01000001234" not in static
    assert static == policy.build_static_prompt(template, phrases, RULES, "2026-12-01 09:00")  # same every call
    context = policy.build_context(start)
    assert "2026-11-21 11:00" in context and "01000001234" in context and "안녕하세요" in context
    llm = ScriptLLM([("text", "네.")])
    run(Dialog(llm, FakeTools(), static, CFG, PHRASES, context=context), "여보세요")
    roles = [m["role"] for m in llm.seen[0]]
    assert roles[:2] == ["system", "user"]  # one system message (two made the model repeat itself)
    sent = llm.seen[0][0]["content"]
    assert sent.startswith(static) and sent.endswith(context)  # static prefix first for the cache
