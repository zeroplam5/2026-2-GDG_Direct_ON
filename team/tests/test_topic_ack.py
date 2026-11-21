"""Topic acknowledgement ("말씀하신 배송 상황 확인해 보겠습니다.") and how it works with the cushion phrase.
Test doubles only (fake STT, ScriptedLLM with a delay, ToneTTS) - no API keys."""
import pytest

from team.agent.config import Config
from team.agent.topic_ack import TOPICS, last_agent_line, pick

from .helpers import run_client, silence, speechlike
from .test_agent_protocol import TEXTS, _metrics, agent, run, two_mocks  # noqa: F401  (fixtures)

DELIVERY = TOPICS["delivery"][1]
PHRASES = Config().cushion_text.split("|")


@pytest.mark.parametrize("text,last,caller,want", [
    ("주문번호 20261028-0009581 배송 어디쯤이에요?", None, False, "delivery"),
    ("010-1234-5678로 주문한 거 반품하고 싶어요", None, False, "return"),
    ("교환 신청한 거 어떻게 됐어요?", None, True, "exchange"),           # no number, but caller info
    ("공일공 일이삼사 오육칠팔 주문 취소해 주세요", None, False, "cancel"),  # spoken digits
    ("환불 언제 들어오나요 주문번호 12345678", None, False, "refund"),
])
def test_picks_one_clear_topic(text, last, caller, want):
    got = pick(text, last, caller)
    assert got and got[0] == want and got[1].startswith("말씀하신")


@pytest.mark.parametrize("text,last,caller", [
    ("주문번호 12345678 반품 말고 교환하고 싶어요", None, False),        # correction
    ("주문번호 12345678 반품하고 환불 받고 싶어요", None, False),        # two topics
    ("배송 언제 와요?", None, False),                                   # nothing to look up with
    ("네 배송 확인해 주세요 12345678", None, False),                     # short-reply opening
    ("네", None, True),
    ("주문번호 12345678 배송 확인해 주세요", "주문번호를 말씀해 주시겠어요?", False),  # answering a question
    ("주문번호 12345678 배송 안 왔어요", None, False),                   # negation
    ("상품 성분이 궁금한데 12345678", None, True),                       # no lookup topic
])
def test_stays_quiet_when_unsure(text, last, caller):
    assert pick(text, last, caller) is None


def test_handoff_flags_and_history_helpers():
    assert pick("주문번호 12345678 배송 확인해 주세요", None, False, flags=["angry"]) is None
    hist = [{"role": "customer", "text": "안녕하세요"}, {"role": "agent", "text": "무엇을 도와드릴까요?"},
            {"role": "tool_call", "name": "get_order_info", "arguments": {}}]
    assert last_agent_line(hist) == "무엇을 도와드릴까요?" and last_agent_line(None) is None


def test_defaults_and_env(monkeypatch):
    assert Config().topic_ack is True
    monkeypatch.setenv("AGENT_TOPIC_ACK", "0")
    assert Config.from_env().topic_ack is False


def _said(r):
    return [e["text"] for e in r["events"] if e.get("type") == "agent.transcript"]


def _go(start, sid, murl, case_id="ORDER_OK"):
    async def go():
        h = await start()
        return await run_client(h["url"], sid, speechlike(1.5) + silence(0.3), murl, case_id=case_id, speed=1)
    return run(go())


def test_topic_line_first_then_answer_and_history(agent, two_mocks):
    start, cfg = agent
    cfg.send_transcript_event = True
    cfg.scripted_delay_ms = 1500
    cfg.cushion_after_s = 5.0
    cfg.tool_ack = True                     # would add "확인해 보겠습니다" - must not, the line is already said
    cfg.log_text = True
    (store, murl), _ = two_mocks
    assert pick(TEXTS["ORDER_OK"], None, False)[0] == "delivery"   # the sample case is a delivery question
    r = _go(start, "SID-TOPIC-1", murl)
    said = _said(r)
    assert r["turn_end"] and said[0] == DELIVERY and said.count(DELIVERY) == 1
    assert any("배송중" in s for s in said[1:])
    assert not any(s in ("확인해 보겠습니다.", "잠시만요, 조회해 볼게요.", "네, 바로 찾아보겠습니다.") for s in said)
    assert not set(said) & set(PHRASES)            # answer came within 5 s of the line: no cushion
    row = [x for x in _metrics(cfg) if x["session_id"] == "SID-TOPIC-1"][-1]
    assert row["topic_ack"] == "delivery" and row["cushion"] is False
    assert row["reply_text"].startswith(DELIVERY)  # the line is part of the reply in the history
    assert row["first_audio_ms_rel"] / 1000 < 1.5  # first sound right after the transcript


def test_cushion_counts_from_the_end_of_the_topic_line(agent, two_mocks):
    """The commit comes ~1.1 s after the speech, the line right after it (~1 s of tone); cushion_after
    2 s; answer after 6 s: the cushion comes 2 s after the line ends, not 2 s after the speech."""
    start, cfg = agent
    cfg.send_transcript_event = True
    cfg.scripted_delay_ms = 6000
    cfg.cushion_after_s = 2.0
    cfg.log_text = True
    (store, murl), _ = two_mocks
    r = _go(start, "SID-TOPIC-CUSH", murl)
    said = _said(r)
    assert said[0] == DELIVERY and said[1] in PHRASES and any("배송중" in s for s in said[2:])
    row = [x for x in _metrics(cfg) if x["session_id"] == "SID-TOPIC-CUSH"][-1]
    assert row["topic_ack"] == "delivery" and row["cushion"] is True
    # the topic line is the first sound (right after the transcript), the cushion is not
    assert row["first_audio_ms_rel"] / 1000 < 1.6


def test_off(agent, two_mocks):
    start, cfg = agent
    cfg.topic_ack = False
    cfg.send_transcript_event = True
    cfg.scripted_delay_ms = 50
    (store, murl), _ = two_mocks
    r = _go(start, "SID-TOPIC-OFF", murl)
    assert r["turn_end"] and DELIVERY not in _said(r)
