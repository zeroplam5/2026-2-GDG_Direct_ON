"""Evidence field (근거 칸, cfg.gate_evidence): the model quotes, per value of a registration, the
customer's words it came from; the gate checks the quotes. Made-up values only."""
import json

from team.agent.config import Config
from team.agent.gate import EVIDENCE_FIELD, Gate
from team.agent.policy import load_rules

RULES = load_rules(Config().rules_path)  # official pack
TOOLS = [{"type": "function", "function": {"name": n, "parameters": {
    "type": "object", "required": req, "properties": {a: {"type": "string"} for a in req}}}}
    for n, req in [("get_order_info", ["order_id"]), ("return_order", ["order_number", "reason"]),
                   ("request_callback", ["customer_name", "phone_number"])]]
ORD, ORD2 = "20990101-0001234", "20990102-0005678"


def gate(history=None, evidence=True):
    g = Gate(RULES, {"history": history or [], "caller": None}, evidence=evidence)
    g.augment(TOOLS)
    return g


def call(g, name, utterance, evidence=None, **kw):
    a = dict(kw)
    if evidence is not None:
        a[EVIDENCE_FIELD] = evidence
    return g.check(name, json.dumps(a, ensure_ascii=False), utterance)


def test_field_added_to_writes_only():
    out = {s["function"]["name"]: s["function"]["parameters"] for s in gate().augment(TOOLS)}
    assert EVIDENCE_FIELD in out["return_order"]["properties"]
    assert EVIDENCE_FIELD in out["return_order"]["required"]
    assert EVIDENCE_FIELD not in out["get_order_info"]["properties"]
    off = {s["function"]["name"]: s["function"]["parameters"] for s in gate(evidence=False).augment(TOOLS)}
    assert EVIDENCE_FIELD not in off["return_order"]["properties"]


def test_quoted_reason_passes_and_field_is_stripped():
    g = gate([{"role": "customer", "text": "먹어도 효과가 하나도 없어서요."}])
    d = call(g, "return_order", f"주문번호 {ORD} 전체 반품해 주세요.",
             {"order_number": f"주문번호 {ORD}", "reason": "먹어도 효과가 하나도 없어서요"},
             order_number=ORD, reason="효과 없음")
    assert d.allowed, d.message
    assert EVIDENCE_FIELD not in d.args
    assert d.evidence == {"order_number": "ok", "reason": "ok"}


def test_invented_reason_is_blocked():
    g = gate()
    d = call(g, "return_order", f"주문번호 {ORD} 반품해 주세요.", {"order_number": f"주문번호 {ORD}"},
             order_number=ORD, reason="단순 변심")
    assert not d.allowed and d.reason == "no_evidence"
    assert "사유" in d.message


def test_reason_from_caller_record_is_not_evidence():
    g = gate()
    d = call(g, "return_order", f"{ORD} 반품이요.", {"reason": "조회 결과"}, order_number=ORD, reason="배송 지연")
    assert not d.allowed and d.reason == "no_evidence"


def test_paraphrased_reason_without_quote_passes_by_shared_word():
    g = gate([{"role": "customer", "text": "아침 저녁으로 먹어도 효과가 없어요. 반품하려고요."}])
    d = call(g, "return_order", f"네 {ORD} 전체로 해 주세요.", None, order_number=ORD,
             reason="효과가 없어서 반품하고 싶어요")
    assert d.allowed, d.message
    assert d.evidence["reason"] == "fallback"


def test_number_outside_its_quote_is_blocked():
    g = gate([{"role": "customer", "text": f"주문번호는 {ORD2}예요. 효과가 없어서요."}])
    d = call(g, "return_order", f"{ORD} 이것도요. 효과가 없어서요",
             {"order_number": f"주문번호는 {ORD2}예요", "reason": "효과가 없어서요"}, order_number=ORD, reason="효과 없음")
    assert not d.allowed and d.reason == "evidence_mismatch"


def test_corrected_number_is_blocked_and_the_new_one_passes():
    utt = "상담원 전화 부탁해요. 제 번호는 010-9999-0000이요. 아 잘못 말했다, 010-9999-0001이에요. 이름은 홍길동이요."
    g = gate()
    old = call(g, "request_callback", utt, {"phone_number": "010-9999-0000", "customer_name": "이름은 홍길동이요"},
               phone_number="01099990000", customer_name="홍길동")
    assert not old.allowed and old.reason == "corrected"
    new = call(g, "request_callback", utt, {"phone_number": "010-9999-0001이에요", "customer_name": "이름은 홍길동이요"},
               phone_number="01099990001", customer_name="홍길동")
    assert new.allowed, new.message


def test_two_orders_without_correction_both_pass():
    utt = f"{ORD}하고 {ORD2} 두 건 다 반품할게요. 효과가 없어서요."
    g = gate()
    for o in (ORD, ORD2):
        d = call(g, "return_order", utt, {"order_number": o, "reason": "효과가 없어서요"}, order_number=o, reason="효과 없음")
        assert d.allowed, (o, d.message)


def test_field_without_underscore_is_stripped_too():
    g = gate()
    a = {"customer_name": "홍길동", "phone_number": "01099990001",
         "evidence": {"customer_name": "홍길동이요", "phone_number": "010-9999-0001"}}
    d = g.check("request_callback", json.dumps(a, ensure_ascii=False),
                "상담원 전화 부탁해요, 010-9999-0001 홍길동이요.")
    assert d.allowed, d.message
    assert "evidence" not in d.args and d.evidence["customer_name"] == "ok"


def test_unfound_number_quote_is_only_logged():
    g = gate()
    d = call(g, "return_order", f"{ORD} 반품이요, 맛이 이상해서요.", {"order_number": "주문번호 말씀드린 거요",
                                                                     "reason": "맛이 이상해서요"},
             order_number=ORD, reason="맛 이상")
    assert d.allowed, d.message
    assert d.evidence["order_number"] == "quote_not_found"
