"""Output check cases from the 2026-11-21 callback experiment (starter_v2 DEMO2/DEMO3 + team K set,
gpt-4.1 and gpt-5.4-mini, 2 runs each). Sentences below are what the models actually said."""
import asyncio
import json

from team.agent import policy
from team.agent.config import Config
from team.agent.dialog import Dialog
from team.agent.gate import CONFIRM_FIELD, Gate
from team.agent.output_guard import OutputGuard
from team.agent.providers.llm import ToolCall

CFG = Config()
RULES = policy.load_rules(CFG.rules_path)
PHRASES = policy.load_phrases(CFG.phrases_path)
CALLER = {"name": "데모 고객", "phone": "01000001234", "email": None}
CB_OK = json.dumps({"status": "ok", "message": "상담원 콜백 접수"}, ensure_ascii=False)
SRC = "발신자 " + json.dumps(CALLER, ensure_ascii=False) + " 내일 오전에 다시 전화 좀 부탁드려요."


def guard(strict="ids,time", mode="log"):
    return OutputGuard(mode, PHRASES["unverified_fallback"], strict=strict, phrases=PHRASES, rules=RULES)


# ── ids: wrong / made-up phone and order numbers are never said ─────────────
def test_garbled_or_made_up_phone_numbers_are_blocked():
    g = guard()
    for s in ["전화번호 일공일공 공공공일 이공삼사로 맞으시면, 그 번호로 다시 전화 요청을 접수하겠습니다.",  # 5.4-mini DEMO3
              "전화번호는 발신자 정보의 010 공일공공 일이삼사 맞으시면, 맞다고 말씀해 주세요.",          # 5.4-mini K07
              "고객님의 전화번호가 010-1234-5678이 맞는지 확인해 주시겠어요?"]:                      # gpt-4.1 K06
        out, flag = g.check(s, SRC)
        assert out == PHRASES["unverified_id_fallback"] and flag["kind"] == "ungrounded_id", s


def test_correct_numbers_pass():
    g = guard()
    for s in ["010-0000-1234 번호로 상담원 연락을 요청하신다는 말씀 맞으신가요?",
              "010에 0000에 1234번으로 상담원 연락 요청을 접수했습니다.",
              "01000001234 번호로 상담원 연락을 접수해 드렸습니다."]:
        assert g.check(s, SRC) == (s, None), s
    assert g.check("주문번호 데모 일공공일 배송 상태는 배송중입니다.", '{"order_id": "DEMO1001"}') \
        == ("주문번호 데모 일공공일 배송 상태는 배송중입니다.", None)


def test_made_up_order_id_is_blocked_but_other_numbers_only_logged():
    g = guard()
    out, flag = g.check("주문번호 DEMO1002 건은 배송중입니다.", '{"order_id": "DEMO1001"}')
    assert out == PHRASES["unverified_id_fallback"] and flag["values"] == ["1002"]
    out, flag = g.check("반품은 14일 이내 가능합니다.", "매뉴얼: 반품은 7일 이내")
    assert out == "반품은 14일 이내 가능합니다." and flag["kind"] == "ungrounded_number"  # mode log


def test_strict_off_keeps_the_old_log_only_behaviour():
    out, flag = guard(strict="").check("고객님의 전화번호가 010-1234-5678이 맞는지 확인해 주시겠어요?", SRC)
    assert out.startswith("고객님의") and flag["kind"] == "ungrounded_number" and flag["action"] == "log"


# ── time: no contact time is promised that no function result carries ──────
def test_time_promises_without_a_time_in_the_result_are_replaced():
    g = guard()
    for s in ["내일 오전 열 시쯤으로 확인해 두었습니다.",                                  # 5.4-mini DEMO3T
              "내일 오전에 다시 전화 부탁하신 내용으로 접수되었습니다.",                  # 5.4-mini DEMO3C
              "내일 오전 10시쯤 요청 내용으로 전달되었습니다.",                          # 5.4-mini DEMO3T (2nd run)
              "내일 오전 열 시로 메모해 두었습니다.",
              "내일 오전에 연락드리겠습니다.",
              "오후 3시에 전화드릴 예정입니다."]:
        out, flag = g.check(s, SRC + " 10시", results=CB_OK)
        assert out == PHRASES["unconfirmed_time_fallback"] and flag["kind"] == "unconfirmed_time", s


def test_honest_sentences_questions_and_plain_confirmations_pass():
    g = guard()
    for s in ["내일 오전 요청은 반영되지 않았고, 방금 말씀하신 번호로만 접수되었습니다.",           # 5.4-mini, correct
              "내일 오전 10시쯤이라는 시간 지정은 접수에 포함되지 않고, 연락 요청만 등록됩니다.",  # gpt-4.1
              "상담원 연락 요청이 접수되었습니다.",
              "01000001234 번호로 상담원 연락을 접수해 드릴까요?",  # the template question: no time
              "상담원이 순차적으로 연락드릴 예정입니다.",
              "다시 한 번 말씀해 주시겠어요?",
              "오늘 출고되어 배송중입니다."]:
        assert g.check(s, SRC + " 10시", results=CB_OK) == (s, None), s


def test_a_question_offering_a_time_is_kept_with_the_note_said_first():
    g = guard()
    note = PHRASES["unconfirmed_time_note"]
    for q in ["내일 오전 10시에 010-0000-1234 번호로 상담원 연락을 요청하신 것으로 접수해도 괜찮을까요?",  # gpt-4.1 DEMO3T
              "상담원 연락 요청을 010-0000-1234 번호로 내일 오전 10시쯤 접수해 드릴까요?",              # gpt-4.1 DEMO3T
              "내일 오전으로 전화 요청을 접수하려면 휴대전화 번호를 다시 한번 말씀해 주시겠습니까?"]:     # 5.4-mini DEMO3
        out, flag = g.check(q, SRC + " 10시", results=CB_OK)
        assert out == f"{note} {q}" and flag == {"kind": "unconfirmed_time_question",
                                                 "values": [flag["values"][0]], "action": "note"}, q
    # once per turn: not again after the note or the statement fallback was said
    q = "내일 오전에 연락드려도 될까요?"
    assert g.check(q, SRC, [f"{note} 다른 질문?"], CB_OK)[0] == q
    assert g.check(q, SRC, [PHRASES["unconfirmed_time_fallback"]], CB_OK)[0] == q
    # a time in a function result: nothing to add
    assert g.check(q, SRC, (), '{"callback_at": "2026-11-22 10:00"}') == (q, None)


def test_a_time_in_a_function_result_allows_the_time():
    res = json.dumps({"status": "ok", "callback_at": "2026-11-22 10:00"})
    s = "내일 오전 열 시에 연락드리겠습니다."
    assert guard().check(s, SRC + res, results=res) == (s, None)
    assert guard(strict="ids").check(s, SRC, results=CB_OK) == (s, None)  # time check switched off


# ── dialog: the check sees this session's function results ────────────────
class ScriptLLM:
    def __init__(self, *rounds):
        self.rounds = list(rounds)

    async def stream(self, messages, tools=None, tool_choice="auto"):
        for item in self.rounds.pop(0):
            yield item


class Tools:
    def __init__(self):
        self.sent = []

    def openai_schemas(self):
        return [{"type": "function", "function": {"name": "request_callback", "description": "", "parameters": {
            "type": "object", "required": ["phone_number"], "properties": {"phone_number": {"type": "string"}}}}}]

    async def call(self, name, arguments_json):
        self.sent.append((name, json.loads(arguments_json)))
        return {"status": "ok", "message": "상담원 콜백 접수"}


def _turn(dlg, text):
    m = {}

    async def go():
        return [s async for s in dlg.run_turn(text, m)]
    return asyncio.run(go()), m


def test_second_turn_callback_answer_drops_the_time_promise():
    hist = [{"role": "customer", "text": "내일 오전에 다시 전화 좀 부탁드려요."},
            {"role": "agent", "text": "01000001234 번호로 상담원 연락을 접수해 드릴까요?"}]
    call = ToolCall("c0", "request_callback", json.dumps({"phone_number": "01000001234", CONFIRM_FIELD: "네"}))
    llm = ScriptLLM([("tool_calls", [call])],
                    [("text", "상담원 연락 요청이 접수되었습니다. 내일 오전 열 시쯤으로 확인해 두었습니다.")])
    tools = Tools()
    dlg = Dialog(llm, tools, "system", CFG, PHRASES, gate=Gate(RULES, {"caller": CALLER, "history": hist}),
                 context=policy.build_context({"caller": CALLER, "history": hist}))
    said, m = _turn(dlg, "네, 그 번호로 내일 오전 열 시쯤으로 부탁드려요.")
    assert tools.sent == [("request_callback", {"phone_number": "01000001234"})]
    assert said == ["상담원 연락 요청이 접수되었습니다.", PHRASES["unconfirmed_time_fallback"]]
    assert [f["kind"] for f in m["output_flags"]] == ["unconfirmed_time"]


def test_first_turn_read_back_comes_from_the_template_with_the_exact_number():
    # the model calls without consent as the generated rules say; the gate blocks it (nothing sent)
    # and the code reads the number back - the model never writes the digits itself
    llm = ScriptLLM([("tool_calls", [ToolCall("c0", "request_callback",
                                              json.dumps({"phone_number": "01000001234", CONFIRM_FIELD: ""}))])])
    tools = Tools()
    dlg = Dialog(llm, tools, "system", CFG, PHRASES, gate=Gate(RULES, {"caller": CALLER, "history": []}))
    said, m = _turn(dlg, "내일 오전에 다시 전화 좀 부탁드려요.")
    assert tools.sent == []
    assert said == ["01000001234 번호로 상담원 연락을 접수해 드릴까요?"] and m["blocked_reply"] == "template"
    assert m["gate"] == [{"name": "request_callback", "allowed": False, "reason": "no_consent"}]
