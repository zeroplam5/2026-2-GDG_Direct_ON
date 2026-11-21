"""Official pack (2026-11-21 day kit): history shape, brand material, the gate's "context" mode
and the output check fixes. All names / numbers below are made up (the kit data is not copied)."""
import json

from team.agent import policy
from team.agent.config import OFFICIAL_PACK, Config, repo_path
from team.agent.gate import Gate
from team.agent.output_guard import OutputGuard, numbers_in
from team.agent.providers.llm import is_reasoning_model, model_params

RULES = policy.load_rules(str(OFFICIAL_PACK / "rules.json"))
OFFICIAL_FUNCTIONS = {"get_order_info", "get_return_info", "search_products", "cancel_order",
                      "return_order", "return_order_item", "exchange_order", "request_callback"}
ORDER = "20990101-0001234"
ITEM = "20990101-0001234-01"
LOOKUP = {"status": "ok", "data": {"orders": [{"주문번호": ORDER, "상세주문번호": ITEM, "배송상태": "배송완료"}]}}
HIST_RETURN = [
    {"role": "customer", "text": "먹어 봤는데 효과가 없어서 반품하고 싶어요 🥲"},
    {"role": "agent", "text": "주문번호나 전화번호 말씀해 주시겠어요? <link>https://example.com/x</link>"},
    {"role": "customer", "text": "010 9999 0000"},
    {"role": "tool_call", "name": "get_order_info", "arguments": {"buyer_cellphone": "01099990000"}},
    {"role": "tool_result", "name": "get_order_info", "result": LOOKUP},
    {"role": "agent", "text": "전체 반품으로 진행할까요, 일부만 반품할까요?"},
]


def gate(history=(), caller=None):
    return Gate(RULES, {"history": list(history), "caller": caller})


def args(**kw):
    return json.dumps(kw, ensure_ascii=False)


# ── defaults: the official pack is what runs ─────────────────────────────
def test_official_pack_is_the_default():
    cfg = Config()
    assert cfg.rules_path == str(OFFICIAL_PACK / "rules.json")
    assert cfg.prompt_path == str(OFFICIAL_PACK / "prompt.md")
    assert cfg.llm_model == "gpt-5.4-mini"
    assert repo_path(cfg.brand_dir).name == "brand"
    for p in (cfg.prompt_path, cfg.rules_path, cfg.phrases_path):
        assert open(p, encoding="utf-8").read()


def test_all_eight_official_functions_have_a_rule():
    assert set(RULES["functions"]) == OFFICIAL_FUNCTIONS
    reads = {n for n, s in RULES["functions"].items() if s["kind"] == "read"}
    assert reads == {"get_order_info", "get_return_info", "search_products"}
    for n, s in RULES["functions"].items():
        if s["kind"] == "write":
            assert s["confirm"] == "context" and s.get("intent") and s.get("value_args"), n


def test_context_mode_adds_no_internal_field_to_the_schemas():
    schemas = [{"type": "function", "function": {"name": n, "parameters": {"type": "object", "properties": {}}}}
               for n in OFFICIAL_FUNCTIONS]
    assert gate().augment(schemas) == schemas  # the model sees the official definitions unchanged


def test_router_prefixed_model_is_a_reasoning_model():
    assert is_reasoning_model("openai/gpt-5.4-mini")
    assert model_params(Config(llm_model="openai/gpt-5.4-mini")).get("reasoning_effort") == "none"


# ── history: official shape with function entries ───────────────────────
def test_history_keeps_function_entries_and_cleans_chat_text():
    turns = policy.normalize_history(HIST_RETURN)
    assert [r for r, _ in turns] == ["customer", "agent", "customer", "tool_call", "tool_result", "agent"]
    assert "🥲" not in turns[0][1] and "https://" not in turns[1][1] and "(링크)" in turns[1][1]
    assert turns[4][1].startswith("get_order_info ") and ORDER in turns[4][1]
    text = policy.format_history(HIST_RETURN)
    assert "고객: 010 9999 0000" in text
    assert '(함수 호출) get_order_info {"buyer_cellphone": "01099990000"}' in text
    assert "(함수 결과) get_order_info" in text and ORDER in text


def test_unknown_history_entries_are_skipped_not_the_whole_history():
    turns = policy.normalize_history([{"role": "customer", "text": "배송 언제 와요"}, {"x": 1}, "junk"])
    assert turns == [("customer", "배송 언제 와요")]


def test_practice_sized_history_is_not_cut():
    # the longest practice history is ~5,100 characters: kept whole
    tail = "제조번호 1234567 소비기한 2099.01.01"
    hist = [{"role": "customer", "text": "0번째 말 주문번호 20990101-0001234"}] + \
           [{"role": "customer", "text": f"{i}번째 말 " + "가" * 700 + (tail if i == 7 else "")} for i in range(1, 8)]
    text = policy.format_history(hist)
    assert "20990101-0001234" in text and text.endswith(tail) and "생략" not in text


def test_over_the_limit_whole_old_lines_are_dropped():
    tail = "제조번호 1234567 소비기한 2099.01.01"
    hist = [{"role": "customer", "text": "0번째 말 주문번호 20990101-0001234"}] + \
           [{"role": "customer", "text": f"{i}번째 말 " + "가" * 700 + (tail if i == 39 else "")} for i in range(1, 40)]
    text = policy.format_history(hist)
    assert len(text) <= policy.HISTORY_MAX_CHARS + 20 and text.startswith("(앞선 대화 생략)")
    assert "20990101-0001234" not in text and text.endswith(tail)
    assert all(ln.startswith(("고객: ", "(앞선")) for ln in text.splitlines())  # no line cut in the middle
    # one very long result is still kept whole, never cut in the middle
    result = {"status": "ok", "data": {"orders": [{"주문번호": f"20990101-{i:07d}"} for i in range(600)]}}
    text = policy.format_history([{"role": "tool_result", "name": "get_order_info", "result": result}])
    assert "20990101-0000000" in text and "20990101-0000599" in text


# ── brand material ───────────────────────────────────────────────────────
def test_brand_material_is_appended_after_our_instructions(tmp_path):
    (tmp_path / "scenarios.md").write_text("# 시나리오\n## 1. 배송\n- 주문을 조회한다.", encoding="utf-8")
    (tmp_path / "information.json").write_text(json.dumps(
        [{"tag": "[운영 시간]", "content": "평일 오전 9시~오후 6시 <link>https://example.com</link>"}],
        ensure_ascii=False), encoding="utf-8")
    (tmp_path / "style.json").write_text(json.dumps({"greeting": "안녕하세요, 테스트몰입니다.", "style": "- 존댓말"},
                                                    ensure_ascii=False), encoding="utf-8")
    brand = policy.load_brand(tmp_path)
    prompt = policy.compose_prompt("지침", brand)
    assert prompt.index("지침") < prompt.index("[상담 시나리오]") < prompt.index("[브랜드 정보") < prompt.index("[말투 규칙]")
    assert "평일 오전 9시~오후 6시" in prompt and "https://" not in prompt and "첫 인사: 안녕하세요, 테스트몰입니다." in prompt
    assert policy.load_brand(tmp_path / "missing") == {} and policy.compose_prompt("지침", {}) == "지침"


def test_function_rules_section_describes_context_mode():
    text = policy.function_rules_text(RULES)
    for name in OFFICIAL_FUNCTIONS:
        assert name in text
    assert "따로 되묻지 말고 바로 호출한다" in text and "이번 말에 처음 조회한 결과의" in text
    assert "주문번호 또는 주문자 전화번호가" in text


# ── gate, context mode: what the official call logs do ───────────────────
def test_callback_with_name_and_number_in_this_utterance_is_sent():
    hist = [{"role": "customer", "text": "전화로 주문하고 싶어요"}, {"role": "agent", "text": "성함과 연락처를 남겨 주세요."}]
    d = gate(hist).check("request_callback", args(phone_number="010-9999-0000", customer_name="홍길동"),
                         "공일공 구구구구 공공공공 홍길동이요")
    assert d.allowed, d.reason


def test_placeholder_values_count_as_missing():
    # "고객님" appears in the agent's greeting, so it looked grounded; "없음"-style values are not numbers
    hist = [{"role": "agent", "text": "안녕하세요, 고객님! 전화 주문은 상담원이 도와드려요."}]
    g = gate(hist, {"name": None, "phone": "01099990000", "email": None})
    d = g.check("request_callback", args(phone_number="01099990000", customer_name="고객님"), "전화 주문하고 싶어요")
    assert d.reason == "missing_value" and "customer_name" in d.message
    d = g.check("request_callback", args(phone_number="발신자 정보 없음", customer_name="홍길동"), "홍길동이요 전화 주세요")
    assert d.reason == "missing_value" and "phone_number" in d.message


def test_callback_may_use_the_caller_number_and_needs_a_name():
    caller = {"name": None, "phone": "01099990000", "email": None}
    hist = [{"role": "agent", "text": "제품은 홈페이지나 전화 주문으로 구매하실 수 있어요."}]
    g = gate(hist, caller)
    assert g.check("request_callback", args(phone_number="010-9999-0000", customer_name="홍길동"),
                   "통화 가능한가요? 홍길동").allowed
    d = g.check("request_callback", args(phone_number="010-9999-0000", customer_name=""), "통화 가능한가요?")
    assert d.reason == "missing_value"


def test_return_uses_the_order_number_from_an_earlier_lookup():
    d = gate(HIST_RETURN).check("return_order", args(order_number=ORDER, reason="효과 없음"), "전체로 해 주세요")
    assert d.allowed, d.reason
    assert d.args == {"order_number": ORDER, "reason": "효과 없음"}  # reason is free text: not checked


def test_partial_return_item_code_must_come_from_the_conversation():
    g = gate(HIST_RETURN)
    ok = g.check("return_order_item", args(return_items=[{"order_item_code": ITEM, "quantity": 1}], reason="일부"),
                 "일부만 반품할게요")
    assert ok.allowed, ok.reason
    bad = g.check("return_order_item", args(return_items=[{"order_item_code": "20990101-0009999-01", "quantity": 1}],
                                            reason="일부"), "일부만 반품할게요")
    assert bad.reason == "not_grounded"


def test_a_registration_needs_an_order_number_in_the_official_format():
    # a waybill / other number that happens to be in the conversation is not an order number
    hist = HIST_RETURN + [{"role": "customer", "text": "[사진 전송] 운송장번호 9999-1234-5678"}]
    d = gate(hist).check("return_order", args(order_number="9999-1234-5678", reason="x"), "전체 환불이요")
    assert d.reason == "invalid_format"
    assert gate(hist).check("return_order", args(order_number=ORDER, reason="x"), "전체 환불이요").allowed


def test_a_number_found_only_in_this_turns_lookup_is_not_registered_yet():
    hist = [{"role": "customer", "text": "반품하고 싶어요"}]
    fresh = json.dumps(LOOKUP, ensure_ascii=False)
    g = gate(hist, {"name": "홍길동", "phone": "01099990000", "email": None})
    d = g.check("return_order", args(order_number=ORDER, reason="변심"), "제 번호로 찾아서 반품해 주세요", turn_results=fresh)
    assert d.reason == "unconfirmed_lookup"
    # a lookup may chain on this turn's results
    assert g.check("get_return_info", args(customer_phone_number="01099990000"), "반품 어떻게 됐어요?").allowed


def test_hesitation_and_missing_request_block_a_write():
    g = gate(HIST_RETURN)
    assert g.check("return_order", args(order_number=ORDER, reason="x"), "잠깐만요, 생각해 볼게요").reason == "hesitation"
    assert g.check("return_order", args(order_number=ORDER, reason="x"), "네?").reason == "puzzled"
    hist = [{"role": "customer", "text": "배송 언제 와요?"},
            {"role": "tool_call", "name": "get_order_info", "arguments": {"order_id": ORDER}},
            {"role": "tool_result", "name": "get_order_info", "result": LOOKUP}]
    assert gate(hist).check("cancel_order", args(order_id=ORDER, reason="x"), "알겠습니다").reason == "no_intent"


def test_a_system_notice_does_not_hide_the_agents_question():
    hist = HIST_RETURN + [{"role": "agent", "text": "30분 이상 문의가 없어 상담이 자동으로 종료됩니다."}]
    assert gate(hist).recent_agent_texts(1) == ["전체 반품으로 진행할까요, 일부만 반품할까요?"]
    assert gate(hist).check("return_order", args(order_number=ORDER, reason="x"), "주소지 동일합니다").allowed


def test_order_lookup_needs_one_of_its_numbers():
    caller = {"name": "홍길동", "phone": "01099990000", "email": None}
    g = gate([], caller)
    assert g.check("get_order_info", args(order_id=None, buyer_cellphone="01099990000"), "입금 확인 부탁드려요").allowed
    assert g.check("get_order_info", args(order_id=None, buyer_cellphone=None), "배송 언제 와요").reason == "missing_value"
    assert g.check("get_order_info", args(order_id="20990101-0005555"), "배송 언제 와요").reason == "not_grounded"
    assert g.check("search_products", args(query="간 건강 제품"), "간에 좋은 거 있어요?").allowed


# ── output check fixes ───────────────────────────────────────────────────
def test_separate_numbers_are_not_glued_into_one_id():
    assert numbers_in("1·2·4·10·16·30박스 옵션이 있어요") == ["10", "16", "30"]
    assert numbers_in("주문번호 20990101-0001234 - 2099-01-01") == ["209901010001234", "20990101"]
    assert numbers_in("공일공 구구구구 공공공공") == ["01099990000"]
    g = OutputGuard("log", strict="ids,time", rules=RULES)
    src = "판매가 12,300원, 옵션 1·2·4·10·16·30박스. " + json.dumps(LOOKUP, ensure_ascii=False)
    for s in ["판매가는 12,300원이고 1·2·4·10·16·30박스 옵션이 있어요.",
              f"주문번호 {ORDER} 건은 배송 완료로 나옵니다."]:
        assert g.check(s, src) == (s, None), s


def test_a_function_call_written_as_text_is_never_spoken():
    g = OutputGuard("log", strict="ids,time", rules=RULES)
    out, flag = g.check('search_products({"query":"카드 결제 방법"})\n네, 카드 결제 가능합니다.', "")
    assert out == "네, 카드 결제 가능합니다." and flag["kind"] == "code_text"
    assert g.check('get_order_info({"order_id": null})', "")[0] == ""
    plain = "결제하기 화면(일반 결제)에서 신용카드를 고르시면 됩니다."
    assert g.check(plain, "") == (plain, None)


def test_only_a_time_the_customer_asked_for_is_checked():
    g = OutputGuard("log", strict="ids,time", rules=RULES)
    done = '{"status": "ok", "message": "고객 정보 접수 성공"}'
    practice = "말씀하신 연락처로 전화 요청을 접수했습니다. 업무 시간에 순서대로, 늦어도 오후 6시 전에는 연락드릴 예정입니다."
    assert g.check(practice, "", results=done, customer="홍길동 010 9999 0000이요") == (practice, None)
    echo = "내일 오전 열 시로 연락드리도록 접수했습니다."
    out, flag = g.check(echo, "", results=done, customer="내일 오전 열 시쯤 전화 주세요")
    assert flag["kind"] == "unconfirmed_time" and out != echo


def test_a_contact_time_stated_by_the_brand_material_is_not_replaced():
    g = OutputGuard("log", strict="ids,time", rules=RULES)
    policy_text = "[브랜드 정보]\n■ 전화 요청\n업무 시간 오전 9시~오후 4시, 늦어도 오후 6시 이내 연락"
    ok = "상담원 전화 요청을 접수했습니다. 늦어도 오후 6시 전에는 연락드릴 예정입니다."
    assert g.check(ok, policy_text, results='{"status": "ok"}', policy=policy_text) == (ok, None)
    bad = "내일 오후 6시에 연락드리겠습니다."  # a day the brand cannot promise
    out, flag = g.check(bad, policy_text, results='{"status": "ok"}', policy=policy_text)
    assert flag["kind"] == "unconfirmed_time" and out != bad
    out, flag = g.check(ok, policy_text, results='{"status": "ok"}')  # no policy given: replaced as before
    assert flag["kind"] == "unconfirmed_time"
