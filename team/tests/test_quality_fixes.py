"""Quality fixes of 2026-11-21 evening: delivery-status rules for registrations, phone numbers read
group by group, no word-for-word repeat of the previous reply. Made-up values only."""
import json

from team.agent.config import Config
from team.agent.gate import Gate, order_statuses
from team.agent.output_guard import OutputGuard
from team.agent.policy import load_rules
from team.agent.textutil import normalize_for_tts

RULES = load_rules(Config().rules_path)  # official pack
ORD = "20990101-0001234"


def lookup(status, order=ORD):
    return {"role": "tool_result", "name": "get_order_info", "result": {"status": "ok", "data": {"orders": [
        {"주문번호": order, "상세주문번호": order.replace("-", "") + "01", "제품명": "하루온 데일리 유산균",
         "배송상태": status}]}}}


def check(name, history, utterance, turn_results="", **args):
    g = Gate(RULES, {"history": history, "caller": None}, evidence=False)
    return g.check(name, json.dumps(args, ensure_ascii=False), utterance, turn_results=turn_results)


def test_order_statuses_reads_lookup_json():
    text = json.dumps(lookup("배송중")["result"], ensure_ascii=False)
    assert order_statuses(ORD, text) == {"배송중"}
    assert order_statuses("2099010100012340 1".replace(" ", ""), text) == {"배송중"}  # item number
    assert order_statuses("20990101-0009999", text) == set()


def test_return_of_order_in_transit_is_blocked():
    hist = [{"role": "customer", "text": f"주문번호 {ORD} 반품하고 싶어요. 효과가 없어서요."}, lookup("배송중")]
    d = check("return_order", hist, "네 전체 반품해 주세요.", order_number=ORD, reason="효과 없음")
    assert not d.allowed and d.reason == "wrong_status"
    assert "받으신 뒤" in d.message


def test_return_after_delivery_and_without_lookup_pass():
    hist = [{"role": "customer", "text": f"주문번호 {ORD} 반품하고 싶어요. 효과가 없어서요."}]
    assert check("return_order", hist + [lookup("배송완료")], "네 전체 반품해 주세요.",
                 order_number=ORD, reason="효과 없음").allowed
    assert check("return_order", hist, "네 전체 반품해 주세요.", order_number=ORD, reason="효과 없음").allowed


def test_status_from_this_turns_lookup_counts():
    hist = [{"role": "customer", "text": "효과가 없어서 반품하려고요."}]
    fresh = json.dumps(lookup("배송중")["result"], ensure_ascii=False)
    d = check("return_order", hist, f"주문번호 {ORD} 반품해 주세요.", turn_results=fresh, order_number=ORD,
              reason="효과 없음")
    assert not d.allowed and d.reason == "wrong_status"


def test_cancel_after_shipping_preparation_is_blocked():
    hist = [{"role": "customer", "text": f"{ORD} 주문 취소할게요, 잘못 주문했어요."}, lookup("배송준비중")]
    d = check("cancel_order", hist, "네 취소해 주세요.", order_id=ORD, reason="잘못 주문")
    assert not d.allowed and d.reason == "wrong_status"
    hist[1] = lookup("상품준비중")
    assert check("cancel_order", hist, "네 취소해 주세요.", order_id=ORD, reason="잘못 주문").allowed


def test_phone_numbers_are_read_group_by_group():
    assert normalize_for_tts("010-9999-0001로 연락드릴게요") == "공 일 공, 구 구 구 구, 공 공 공 일로 연락드릴게요"
    assert normalize_for_tts("고객센터 02-000-0000") == "고객센터 공 이, 공 공 공, 공 공 공 공"
    assert normalize_for_tts("가격은 29,000원") == "가격은 29,000원"


def test_previous_reply_sentence_is_not_repeated():
    g = OutputGuard()
    earlier = ["확인해 보니 배송중입니다.", "택배사 홈페이지에서 송장번호로 배송을 조회하실 수 있습니다."]
    s = "택배사 홈페이지에서 송장번호로 배송을 조회하실 수 있습니다."
    out, flag = g.check(s, "", spoken=["네, 확인했습니다."], earlier=earlier)
    assert out == "" and flag["kind"] == "repeated_from_last_turn"
    # never the first sentence of a turn (the turn must not go silent), never when nothing was said before
    assert g.check(s, "", spoken=[], earlier=earlier)[0] == s
    assert g.check(s, "", spoken=["네."], earlier=[])[0] == s


# ── dialog: lookup + registration in the same round ─────────────────────────
import asyncio  # noqa: E402

from team.agent.dialog import Dialog  # noqa: E402
from team.agent.providers.llm import ToolCall  # noqa: E402


class _LLM:
    """Round 1: order lookup and return in one round (as gpt-5.4-mini did). Round 2: answers what it sees."""

    def __init__(self):
        self.rounds = []

    async def stream(self, messages, tools=None, tool_choice="auto"):
        self.rounds.append([dict(m) for m in messages])
        if len(self.rounds) == 1:
            yield ("tool_calls", [ToolCall("c1", "get_order_info", json.dumps({"order_id": ORD})),
                                  ToolCall("c2", "return_order", json.dumps({"order_number": ORD, "reason": "두드러기"},
                                                                            ensure_ascii=False))])
            return
        yield ("text", "배송중이라 받으신 뒤 반품 접수가 가능합니다. ")


class _Tools:
    def __init__(self, status):
        self.status, self.sent = status, []

    def openai_schemas(self):
        return [{"type": "function", "function": {"name": n, "parameters": {"type": "object", "properties": {}}}}
                for n in ("get_order_info", "return_order")]

    async def call(self, name, arguments_json):
        self.sent.append(name)
        await asyncio.sleep(0.05)
        if name == "get_order_info":
            return lookup(self.status)["result"]
        return {"status": "ok", "message": "주문 반품 접수 성공"}


def _turn(status):
    cfg = Config(prefetch=False, write_answer_ahead=True)
    hist = [{"role": "customer", "text": "이거 먹고 두드러기가 났어요."}]
    tools = _Tools(status)
    dlg = Dialog(_LLM(), tools, "지침", cfg, {"tool_round_limit": "x"},
                 gate=Gate(RULES, {"history": hist}, evidence=True))

    async def go():
        m = {}
        out = [s async for s in dlg.run_turn(f"그럼 환불해 주세요. 주문번호 {ORD}요.", m)]
        return m, out
    m, out = asyncio.run(go())
    return tools, m


def test_parallel_lookup_runs_first_and_blocks_return_in_transit():
    tools, m = _turn("배송중")
    assert tools.sent == ["get_order_info"]                      # the return was never sent
    assert [g["reason"] for g in m["gate"]] == [None, "wrong_status"]


def test_parallel_lookup_then_return_when_delivered():
    tools, m = _turn("배송완료")
    assert tools.sent == ["get_order_info", "return_order"]      # lookup first, then the registration
