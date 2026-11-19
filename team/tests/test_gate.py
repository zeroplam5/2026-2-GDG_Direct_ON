"""Pre-execution gate (team/agent/gate.py): code-level checks before a function call is sent.

Cases follow the team's confirmation table (C1..C10): consent needs action + values + agreement,
judged by a hybrid check (the model quotes the consent, the code verifies it).
"""
import asyncio
import json

from team.agent.config import Config
from team.agent.dialog import Dialog
from team.agent.gate import CONFIRM_FIELD, Gate, normalize_text
from team.agent.policy import load_rules
from team.agent.providers.llm import ToolCall

POLICY = load_rules(Config().rules_path)
CALLER = {"name": "연습 고객", "phone": "01000001234", "email": None}
ASK = "지금 거신 번호 01000001234로 내일 연락드려도 될까요?"
TOOLS = [
    {"type": "function", "function": {"name": "get_order_info", "description": "",
     "parameters": {"type": "object", "required": ["order_id"], "properties": {"order_id": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "request_callback", "description": "",
     "parameters": {"type": "object", "required": ["phone_number"], "properties": {"phone_number": {"type": "string"}}}}},
]


def gate(history=None, caller=CALLER):
    g = Gate(POLICY, {"history": history or [], "caller": caller})
    g.augment(TOOLS)
    return g


def cb(phone="01000001234", quote=None):
    args = {"phone_number": phone}
    if quote is not None:
        args[CONFIRM_FIELD] = quote
    return json.dumps(args, ensure_ascii=False)


def asked():
    return [{"role": "customer", "text": "내일 콜백 부탁드려요."}, {"role": "agent", "text": ASK}]


# ── normalisation ─────────────────────────────────────────────────────────
def test_korean_digit_words_become_digits():
    assert "01000001234" in normalize_text("공일공 공공공공 일이삼사요")
    assert "DEMO1001" in normalize_text("주문번호 데모 일 공 공 일 이에요")
    assert normalize_text("이에요") == "이에요"  # fewer than 3 digit words stay text


# ── schema augmentation ───────────────────────────────────────────────────
def test_augment_adds_consent_field_to_write_functions_only():
    out = {s["function"]["name"]: s["function"] for s in Gate(POLICY).augment(TOOLS)}
    assert CONFIRM_FIELD in out["request_callback"]["parameters"]["properties"]
    assert CONFIRM_FIELD in out["request_callback"]["parameters"]["required"]
    assert CONFIRM_FIELD not in out["get_order_info"]["parameters"]["properties"]
    assert CONFIRM_FIELD not in TOOLS[1]["function"]["parameters"]["properties"]  # input untouched


# ── read functions ────────────────────────────────────────────────────────
def test_read_with_grounded_order_id_passes():
    d = gate(caller=None).check("get_order_info", '{"order_id":"DEMO1001"}', "주문번호 데모 일 공 공 일 배송 어디쯤이에요?")
    assert d.allowed and d.args == {"order_id": "DEMO1001"}


def test_read_with_value_not_in_conversation_is_blocked():
    d = gate(caller=None).check("get_order_info", '{"order_id":"DEMO10001"}', "주문번호 데모 일 공 공 일 이요")
    assert not d.allowed and d.reason == "not_grounded"


# ── write functions: C1..C10 ──────────────────────────────────────────────
def test_c1_agreement_to_the_asked_values_passes_and_strips_internal_field():
    d = gate(asked()).check("request_callback", cb(quote="네, 그렇게 해 주세요"), "네, 그렇게 해 주세요.")
    assert d.allowed
    assert d.args == {"phone_number": "01000001234"}  # internal field never reaches tools_url


def test_c2_correction_is_not_consent():
    d = gate(asked()).check("request_callback", cb(quote="해 주세요"), "아니요, 공일공 구팔칠육 오사삼이로 해 주세요.")
    assert not d.allowed and d.reason == "hesitation"


def test_c3_k07_caller_number_without_question_is_blocked():
    d = gate().check("request_callback", cb(quote=""), "내일 오전에 다시 전화 주실 수 있나요?")
    assert not d.allowed and d.reason == "no_consent"
    d = gate().check("request_callback", cb(quote="내일 오전에 다시 전화 주실 수 있나요?"), "내일 오전에 다시 전화 주실 수 있나요?")
    assert not d.allowed and d.reason == "no_consent"  # a request is not agreement to these values


def test_c4_explicit_request_still_needs_read_back_in_explicit_mode():
    d = gate(caller=None).check("request_callback", cb("01012345678", "부탁드려요"), "공일공 일이삼사 오육칠팔로 콜백 부탁드려요.")
    assert not d.allowed and d.reason == "not_asked"


def test_c4_request_mode_accepts_values_stated_in_this_utterance():
    table = {"functions": {"request_callback": {"kind": "write", "confirm": "request", "value_args": ["phone_number"]}}}
    g = Gate(table, {"history": [], "caller": None})
    d = g.check("request_callback", cb("01012345678", "부탁드려요"), "공일공 일이삼사 오육칠팔로 콜백 부탁드려요.")
    assert d.allowed


def test_c7_hesitation_blocks_even_with_a_quote():
    d = gate(asked()).check("request_callback", cb(quote="네"), "음… 잠깐만요, 생각해 볼게요.")
    assert not d.allowed and d.reason == "hesitation"


def test_c8_yes_to_a_different_question_is_blocked():
    hist = [{"role": "agent", "text": "다른 문의 있으세요?"}]
    d = gate(hist).check("request_callback", cb(quote="네"), "네")
    assert not d.allowed and d.reason == "not_asked"


def test_c10_puzzled_yes_is_blocked():
    d = gate(asked()).check("request_callback", cb(quote="네?"), "네?")
    assert not d.allowed and d.reason == "puzzled"


def test_quote_not_in_utterance_is_blocked():
    d = gate(asked()).check("request_callback", cb(quote="네, 접수해 주세요"), "그럼요")
    assert not d.allowed and d.reason == "no_consent"


def test_invalid_phone_format_is_blocked():
    d = gate(asked()).check("request_callback", cb("0100000", "네"), "네")
    assert not d.allowed and d.reason == "invalid_format"


def test_unlisted_function_defaults_to_write_with_consent():
    d = gate(asked()).check("cancel_everything", '{"order_id":"DEMO1001"}', "네")
    assert not d.allowed


# ── dialog integration: a blocked call is never sent ──────────────────────
class FakeLLM:
    def __init__(self):
        self.rounds = 0

    async def stream(self, messages, tools=None, tool_choice="auto"):
        self.rounds += 1
        if self.rounds == 1:
            assert any(CONFIRM_FIELD in t["function"]["parameters"]["properties"] for t in tools)
            yield ("tool_calls", [ToolCall("c0", "request_callback", cb(quote=""))])
        else:
            last = json.loads(messages[-1]["content"])
            assert last["status"] == "not_executed"
            yield ("text", "지금 거신 번호로 내일 연락드려도 될까요?")


class FakeTools:
    def __init__(self):
        self.sent = []

    def openai_schemas(self):
        return TOOLS

    async def call(self, name, arguments_json):
        self.sent.append((name, arguments_json))
        return {"status": "ok"}


def _rules_without_templates(tmp_path):
    rules = json.loads(json.dumps(POLICY))
    for spec in rules["functions"].values():
        spec.pop("confirm_template", None)
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
    return rules, str(p)


def test_dialog_does_not_send_blocked_call_and_model_asks_instead(tmp_path):
    rules, path = _rules_without_templates(tmp_path)  # no confirm_template -> the model words the reply
    cfg = Config(rules_path=path)
    tools = FakeTools()
    g = Gate(rules, {"history": [], "caller": CALLER})
    dlg = Dialog(FakeLLM(), tools, "system", cfg, {"tool_round_limit": "x"}, gate=g)
    m = {}

    async def run():
        return [s async for s in dlg.run_turn("내일 오전에 다시 전화 주실 수 있나요?", m)]

    said = asyncio.run(run())
    assert tools.sent == []
    assert "될까요" in "".join(said)
    assert m["gate"] == [{"name": "request_callback", "allowed": False, "reason": "no_consent"}]
    assert m["blocked_reply"] == "model"
