"""Changes for the official kit (directon-2026-voice-cs, 2026-11-21):
1. history items customer / agent / tool_call / tool_result
2. official pack: write functions use confirm=implicit (no read-back question)
3. mock server: SPEC.md matching rules (read / write / search, "invalid" / "not_found")

The data below is made up in the kit's shapes; no official data is copied into the repo.
The last test replays the kit's own mock_db.json when the kit folder is next to the repo root.
"""
import json
from pathlib import Path

import pytest

from team.agent import policy
from team.agent.gate import CONFIRM_FIELD, Gate
from team.agent.output_guard import OutputGuard
from team.agent.policy import load_rules
from team.mock.mock_api import MockStore

REPO = Path(__file__).resolve().parents[2]
OFFICIAL = load_rules(str(REPO / "team" / "agent" / "packs" / "official" / "official_rules.json"))
KIT = REPO / "directon-2026-voice-cs"

HISTORY = [
    {"role": "customer", "text": "환불하려면 어떻게 해야 돼요?"},
    {"role": "agent", "text": "주문번호나 전화번호 말씀해 주시면 확인 도와드리겠습니다. <link>https://example.com/a"},
    {"role": "customer", "text": "010 5550 8126"},
    {"role": "tool_call", "name": "get_order_info", "arguments": {"buyer_cellphone": "01055508126"}},
    {"role": "tool_result", "name": "get_order_info",
     "result": {"status": "ok", "data": {"orders": [{"주문번호": "20260928-0024417", "배송상태": "배송완료"}]}}},
    {"role": "agent", "text": "전체 반품하실지, 일부만 반품하실지 말씀해 주세요."},
]


# ── 1. history ────────────────────────────────────────────────────────────
def test_history_keeps_every_item_and_tool_results():
    turns = policy.normalize_history(HISTORY)
    assert [r for r, _ in turns] == ["customer", "agent", "customer", "tool_call", "tool_result", "agent"]
    text = policy.format_history(HISTORY)
    assert "함수 호출: get_order_info" in text and "20260928-0024417" in text
    assert "https://" not in text and "(링크)" in text
    assert not text.lstrip().startswith("[")  # not the raw-JSON fallback


def test_history_drops_whole_old_lines_when_too_long():
    long = [{"role": "customer", "text": "가" * 3000}, {"role": "agent", "text": "나" * 3000},
            {"role": "customer", "text": "다" * 3000}]
    text = policy.format_history(long)
    assert text.startswith("(앞선 대화 생략)") and "가" not in text and text.endswith("다" * 3000)


def test_unknown_items_are_skipped_not_the_whole_history():
    turns = policy.normalize_history([{"role": "customer", "text": "안녕하세요"}, {"foo": 1}, "x"])
    assert turns == [("customer", "안녕하세요")]


# ── 2. official pack / gate ───────────────────────────────────────────────
TOOLS = [{"type": "function", "function": {"name": n, "parameters": {"type": "object", "properties": {}}}}
         for n in ("get_order_info", "return_order", "request_callback", "search_products")]


def gate(history=None, caller=None):
    g = Gate(OFFICIAL, {"history": history or [], "caller": caller})
    g.augment(TOOLS)
    return g


def test_implicit_write_functions_get_no_consent_field():
    out = {s["function"]["name"]: s["function"] for s in Gate(OFFICIAL).augment(TOOLS)}
    assert CONFIRM_FIELD not in out["request_callback"]["parameters"]["properties"]
    assert CONFIRM_FIELD not in out["return_order"]["parameters"]["properties"]


def test_callback_with_name_and_number_in_this_utterance_passes():
    # practice P0044-02 style: the customer answers with number + name, no "네"
    args = json.dumps({"phone_number": "010-5182-4529", "customer_name": "염다태"}, ensure_ascii=False)
    d = gate().check("request_callback", args, "010 5182 4529 염다태")
    assert d.allowed, d.reason


def test_callback_number_from_caller_info_passes():
    args = json.dumps({"phone_number": "01049933013", "customer_name": "은하진"}, ensure_ascii=False)
    d = gate(caller={"name": None, "phone": "01049933013"}).check("request_callback", args, "통화 가능한가요? 은하진")
    assert d.allowed, d.reason


def test_callback_with_invented_number_is_blocked():
    args = json.dumps({"phone_number": "01012345678", "customer_name": "홍길동"}, ensure_ascii=False)
    assert gate().check("request_callback", args, "전화 주세요 홍길동이요").reason == "not_grounded"


def test_return_after_answering_full_or_part_passes_with_number_from_history_result():
    args = json.dumps({"order_number": "20260928-0024417", "reason": "효과 없음"}, ensure_ascii=False)
    d = gate(HISTORY).check("return_order", args, "한 포 먹었어요. 전체 환불 원해요.")
    assert d.allowed, d.reason


def test_return_with_number_found_earlier_in_this_turn_passes():
    args = json.dumps({"order_number": "20261001-0060343", "reason": "수량 부족"}, ensure_ascii=False)
    g = gate()
    assert g.check("return_order", args, "전부 반품해 주세요").reason == "not_grounded"
    earlier = json.dumps({"status": "ok", "data": {"orders": [{"주문번호": "20261001-0060343"}]}}, ensure_ascii=False)
    assert g.check("return_order", args, "전부 반품해 주세요", None, earlier).allowed


def test_refusal_still_blocks_implicit_write():
    args = json.dumps({"order_number": "20260928-0024417", "reason": "x"}, ensure_ascii=False)
    assert gate(HISTORY).check("return_order", args, "아니요 괜찮아요, 나중에 할게요").reason == "hesitation"


def test_order_lookup_with_null_phone_is_not_a_format_error():
    args = json.dumps({"order_id": "20260925-0041152", "buyer_cellphone": None})
    assert gate().check("get_order_info", args, "주문번호 20260925-0041152 언제 와요").allowed


def test_search_query_needs_no_grounding():
    args = json.dumps({"query": "간 건강 제품 섭취 방법"}, ensure_ascii=False)
    assert gate().check("search_products", args, "간에 좋다는 거 하루에 한 알 먹으면 돼요?").allowed


def test_prompt_rule_text_for_implicit():
    text = policy.function_rules_text(OFFICIAL)
    assert "request_callback (접수·변경)" in text and "다시 복창해 묻지 말고 바로 호출" in text


def test_business_hours_sentence_passes_time_check_but_a_booked_time_does_not():
    g = OutputGuard("log", strict="ids,time", rules=OFFICIAL)
    ok = "업무 시간인 오전 10시부터 오후 5시 사이에 순서대로 연락드리고 있습니다."
    assert g.check(ok, ok, results='{"status": "ok", "message": "고객 정보 접수 성공"}')[0] == ok
    bad = "내일 오전 열 시에 연락드리겠습니다."
    out, flag = g.check(bad, bad, results='{"status": "ok", "message": "고객 정보 접수 성공"}')
    assert out != bad and flag["kind"] == "unconfirmed_time"


# ── 3. mock server ────────────────────────────────────────────────────────
TOOL_DEFS = [
    {"name": "get_order_info", "parameters": {"type": "object", "properties": {"order_id": {}, "buyer_cellphone": {}}}},
    {"name": "get_return_info", "parameters": {"type": "object", "required": ["customer_phone_number"]}},
    {"name": "search_products", "parameters": {"type": "object", "required": ["query"]}},
    {"name": "return_order", "parameters": {"type": "object", "required": ["order_number", "reason"]}},
    {"name": "return_order_item", "parameters": {"type": "object", "required": ["return_items", "reason"]}},
    {"name": "request_callback", "parameters": {"type": "object", "required": ["customer_name", "phone_number"]}},
]
ORDER = {"status": "ok", "data": {"orders": [{"주문번호": "20260929-0071408"}, {"주문번호": "20260911-0040126"}]},
         "message": "주문 조회 완료"}
DB = {
    "get_order_info": [{"arguments": {"order_id": None, "buyer_cellphone": "010 5550 2714"}, "result": ORDER}],
    "get_return_info": [{"arguments": {"customer_phone_number": "01055504417"},
                         "result": {"status": "not_found", "message": "반품 상품이 없습니다."}}],
    "search_products": [
        {"arguments": {"query": "하루온 간 1회 1알 섭취시기"}, "result": {"status": "ok", "data": {"결과": [{"제품명": "하루온 간 가드 맥스2"}]}}},
        {"arguments": {"query": "혈관 순환 세트"}, "result": {"status": "ok", "data": {"결과": [{"제품명": "혈관 순환 세트"}]}}},
    ],
    "return_order": [
        {"arguments": {"order_number": "20260929-0022095", "reason": "효과 없음"},
         "result": {"status": "already_requested", "message": "주문 반품 접수 실패 - 이미 접수됨"}},
        {"arguments": {"order_number": "20260930-0006855", "reason": "x"}, "result": {"status": "ok", "message": "주문 반품 접수 성공"}},
    ],
}


@pytest.fixture
def store(tmp_path):
    (tmp_path / "tools.json").write_text(json.dumps(TOOL_DEFS), encoding="utf-8")
    (tmp_path / "db.json").write_text(json.dumps(DB, ensure_ascii=False), encoding="utf-8")
    return MockStore(tmp_path / "tools.json", tmp_path / "db.json")


def test_read_by_phone_ignores_hyphens_and_spaces(store):
    assert store.lookup("get_order_info", {"buyer_cellphone": "010-5550-2714"}) == (ORDER, True)
    assert store.lookup("get_order_info", {"order_id": None, "buyer_cellphone": "01055502714"})[0] == ORDER


def test_read_by_order_number_found_inside_a_phone_record(store):
    assert store.lookup("get_order_info", {"order_id": "20260911-0040126"}) == (ORDER, True)


def test_read_not_found_and_invalid(store):
    assert store.lookup("get_order_info", {"buyer_cellphone": "01099999999"})[0]["status"] == "not_found"
    assert store.lookup("get_order_info", {"order_id": None, "buyer_cellphone": ""})[0]["status"] == "invalid"
    assert store.lookup("get_return_info", {})[0]["status"] == "invalid"


def test_recorded_not_found_result_is_returned_as_is(store):
    assert store.lookup("get_return_info", {"customer_phone_number": "010 5550 4417"})[0]["status"] == "not_found"


def test_write_ok_without_record_and_recorded_result_when_present(store):
    r, matched = store.lookup("return_order", {"order_number": "20261111-0000001", "reason": "효과 없음"})
    assert r == {"status": "ok", "message": "주문 반품 접수 성공"} and matched is False
    r, _ = store.lookup("return_order", {"order_number": "20260929 0022095", "reason": "다른 문장"})
    assert r["status"] == "already_requested"
    assert store.lookup("return_order", {"order_number": "20260929-0022095"})[0]["status"] == "invalid"


def test_write_function_without_records_uses_default_message(store):
    r, _ = store.lookup("request_callback", {"customer_name": "정다온", "phone_number": "010-5550-6183"})
    assert r == {"status": "ok", "message": "고객 정보 접수 성공"}
    assert store.lookup("request_callback", {"phone_number": "010-5550-6183"})[0]["status"] == "invalid"
    assert store.lookup("return_order_item", {"return_items": [], "reason": "x"})[0]["status"] == "invalid"


def test_search_returns_closest_record(store):
    assert store.lookup("search_products", {"query": "간 건강 제품 섭취 방법"})[0]["data"]["결과"][0]["제품명"] == "하루온 간 가드 맥스2"
    assert store.lookup("search_products", {"query": "혈관세트 먹는 법"})[0]["data"]["결과"][0]["제품명"] == "혈관 순환 세트"
    assert store.lookup("search_products", {"query": ""})[0]["status"] == "invalid"


@pytest.mark.skipif(not (KIT / "data" / "mock_db.json").is_file(), reason="official kit folder not present")
def test_every_record_of_the_kit_db_is_found_by_its_own_arguments():
    s = MockStore(KIT / "data" / "brand" / "tools.json", KIT / "data" / "mock_db.json")
    assert len(s.tools) == 8
    for name, recs in s.data.items():
        for rec in recs:
            # several records may share a number (the first one wins), so compare the status only
            got, _ = s.lookup(name, rec["arguments"])
            assert got.get("status") == rec["result"].get("status"), (name, rec["arguments"])
