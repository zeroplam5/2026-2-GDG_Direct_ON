"""Gate boundary cases checked while integrating feature/llm-gate into prototype (2026-11-20).

Passing tests = behaviour confirmed in code. xfail(strict=True) = KNOWN GAP: the test states
the desired behaviour, the current gate does not do it yet. When the gate is improved the xfail
turns into an unexpected pass and pytest fails, so the marker must then be removed.
Gaps are listed in team/docs/PROTOTYPE.md ("알려진 위험").
"""
import json

import pytest

from team.agent.config import Config, practice_config
from team.agent.gate import CONFIRM_FIELD, Gate
from team.agent.policy import load_rules

RULES = load_rules(practice_config().rules_path)
TOOLS = [{"type": "function", "function": {"name": n, "parameters": {
    "type": "object", "required": [a], "properties": {a: {"type": "string"}}}}}
    for n, a in [("get_order_info", "order_id"), ("request_callback", "phone_number"),
                 ("cancel_order", "order_id"), ("change_contact", "phone_number")]]
ASK_CB = {"role": "agent", "text": "01000001234 번호로 연락드려도 될까요?"}


def gate(history=None):
    g = Gate(RULES, {"history": history or [], "caller": {"phone": "01000001234"}})
    g.augment(TOOLS)
    return g


def args(quote=None, **kw):
    if quote is not None:
        kw[CONFIRM_FIELD] = quote
    return json.dumps(kw, ensure_ascii=False)


# ── confirmed ─────────────────────────────────────────────────────────────
def test_past_consent_in_history_does_not_count_for_this_utterance():
    h = [{"role": "agent", "text": "주문번호 DEMO1001 주문을 취소해 드릴까요?"}, {"role": "customer", "text": "네"}]
    d = gate(h).check("cancel_order", args("네", order_id="DEMO1001"), "그리고 배송은 언제 와요?")
    assert not d.allowed and d.reason == "no_consent"


def test_yes_to_one_question_does_not_allow_other_values():
    d = gate([ASK_CB]).check("cancel_order", args("네 해주세요", order_id="DEMO1001"),
                             "네 해주세요. 주문번호는 데모 일공공일이요")
    assert not d.allowed and d.reason == "not_asked"


def test_refusal_blocks_even_if_model_quotes_yes():
    d = gate([ASK_CB]).check("request_callback", args("네", phone_number="01000001234"), "아니요 괜찮아요")
    assert not d.allowed and d.reason == "hesitation"


def test_correction_blocks_write_call():
    d = gate([{"role": "agent", "text": "어느 주문을 취소할까요?"}]).check(
        "cancel_order", args("취소해 주세요", order_id="DEMO1002"), "데모 일공공일, 아니 데모 일공공이 취소해 주세요")
    assert not d.allowed  # hesitation word "아니" -> the agent must read the value back first


def test_correction_with_full_new_value_allows_read():
    d = gate().check("get_order_info", args(order_id="DEMO1002"), "주문번호 데모 일공공일, 아니 데모 일공공이요")
    assert d.allowed


# ── known gaps ────────────────────────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="gate checks values in the agent's question, not which action was asked")
def test_gap_same_values_different_action_should_block():
    d = gate([ASK_CB]).check("change_contact", args("네", phone_number="01000001234"), "네")
    assert not d.allowed


@pytest.mark.xfail(strict=True, reason="grounding accepts any value in the utterance, also the one taken back")
def test_gap_read_with_value_before_correction_should_block():
    d = gate().check("get_order_info", args(order_id="DEMO1001"), "주문번호 데모 일공공일, 아니 데모 일공공이요")
    assert not d.allowed


@pytest.mark.xfail(strict=True, reason="'데모' is not carried over to the corrected digits (DEMO1001 ... 1002)")
def test_gap_corrected_value_without_prefix_should_pass():
    d = gate().check("get_order_info", args(order_id="DEMO1002"), "주문번호 데모 일공공일, 아니 일공공이요")
    assert d.allowed
