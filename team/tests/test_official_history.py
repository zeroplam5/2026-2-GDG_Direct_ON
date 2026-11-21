"""Official session.start history shape (SPEC.md, 2026-11-21): customer / agent text turns plus
tool_call / tool_result records without "text". Made-up values only."""
import json
from pathlib import Path

from team.agent import policy
from team.agent.gate import CONFIRM_FIELD, Gate

RULES_PATH = str(Path(__file__).resolve().parents[1] / "agent" / "prompts" / "practice_rules.json")

HISTORY = [
    {"role": "customer", "text": "배송 언제 와요"},
    {"role": "agent", "text": "주문번호나 주문하신 분 연락처를 알려주시겠어요?"},
    {"role": "customer", "text": "공일공 공공공공 일이삼사요"},
    {"role": "tool_call", "name": "get_order_info", "arguments": {"buyer_cellphone": "01000001234"}},
    {"role": "tool_result", "name": "get_order_info",
     "result": {"status": "ok", "data": {"orders": [{"주문번호": "20990101-0000777", "배송상태": "배송중"}]}}},
    {"role": "agent", "text": "주문번호 20990101-0000777 주문을 반품 접수해 드릴까요?"},
]


def test_function_records_do_not_drop_the_history():
    turns = policy.normalize_history(HISTORY)
    assert [r for r, _ in turns] == ["customer", "agent", "customer", "tool_call", "tool_result", "agent"]
    assert "20990101-0000777" in turns[4][1] and turns[3][1].startswith("get_order_info ")
    text = policy.format_history(HISTORY)
    assert "(함수 결과) get_order_info" in text and "상담원: 주문번호 20990101-0000777" in text


def test_gate_uses_numbers_from_tool_results_and_the_last_agent_question():
    rules = policy.load_rules(RULES_PATH)
    g = Gate(rules, {"history": HISTORY, "caller": None})
    args = json.dumps({"order_number": "20990101-0000777", "reason": "변심", CONFIRM_FIELD: "네 해 주세요"})
    d = g.check("return_order", args, "네 해 주세요")
    assert d.allowed, d
    # a number that appears nowhere is still blocked
    bad = json.dumps({"order_number": "20990101-0000999", "reason": "변심", CONFIRM_FIELD: "네 해 주세요"})
    assert not g.check("return_order", bad, "네 해 주세요").allowed


def test_official_read_functions_are_not_blocked_by_default():
    rules = policy.load_rules(RULES_PATH)
    g = Gate(rules, {"history": [], "caller": {"name": "가", "phone": "01000001234", "email": None}})
    assert g.check("search_products", json.dumps({"query": "관절 영양제"}), "관절 영양제 있어요?").allowed
    assert g.check("get_return_info", json.dumps({"customer_phone_number": "01000001234"}), "반품 어떻게 됐어요?").allowed
    assert g.check("get_order_info", json.dumps({"buyer_cellphone": "010-0000-1234"}), "배송 언제 와요?").allowed
