"""Topic acknowledgement: right after the customer's words are transcribed, say one short,
pre-synthesized line that names what they asked about ("말씀하신 배송 상황 확인해 보겠습니다.")
while the model and the lookup are still working.

Used as narrowly as possible, because a wrong topic would be heard (and graded) as a wrong answer,
and the contest site discourages starting every reply with the same filler. ALL of these must hold:
  * exactly one topic word is in the customer's words (배송 / 반품 / 교환 / 취소 / 환불)
  * no negation or correction ("말고", "아니", "않", "안 ", "못 ", "다른", "잘못", "싫")
  * it is a request, not a short reply ("네", "아니요", ... or fewer than 8 characters)
  * the agent's last line in the history was not a question (the customer would be answering it)
  * there is something to look up with: a number of 4+ digits in the words, or caller information
  * no hand-off signal (angry customer, asks for a person: the model handles those)
"""
from __future__ import annotations

import re

# key -> (pattern, line). The lines say only "I will check what you asked about": never a result.
TOPICS: dict[str, tuple[str, str]] = {
    "delivery": (r"배송|택배|도착|출고|언제\s*(?:와|오|받)", "말씀하신 배송 상황 확인해 보겠습니다."),
    "return": (r"반품", "말씀하신 반품 건 확인해 보겠습니다."),
    "exchange": (r"교환", "말씀하신 교환 건 확인해 보겠습니다."),
    "cancel": (r"취소", "말씀하신 주문 취소 건 확인해 보겠습니다."),
    "refund": (r"환불", "말씀하신 환불 건 확인해 보겠습니다."),
}
_NEGATION = re.compile(r"말고|아니|않|안\s|못\s|다른|잘못|싫")
_SHORT_REPLY = re.compile(r"^\s*(?:네|예|응|어|아니요|아뇨|맞아요|맞습니다|그래요|좋아요|괜찮아요)\b")
_DIGIT_KO = "공영일이삼사오육칠팔구"
_NUMBER = re.compile(rf"(?:\d[\d\s-]*){{4,}}|[{_DIGIT_KO}](?:\s*[{_DIGIT_KO}]){{3,}}")
MIN_CHARS = 8


def _digits(text: str) -> int:
    return max((len(re.sub(r"\D", "", m.group())) or len(re.sub(r"\s", "", m.group()))
                for m in _NUMBER.finditer(text or "")), default=0)


def pick(text: str, last_agent: str | None = None, has_caller: bool = False,
         flags: list | None = None) -> tuple[str, str] | None:
    """(topic key, line to say) or None when any condition above does not hold."""
    t = (text or "").strip()
    if len(t) < MIN_CHARS or _SHORT_REPLY.match(t) or _NEGATION.search(t) or flags:
        return None
    if last_agent and last_agent.strip().endswith("?"):
        return None
    hits = [k for k, (p, _) in TOPICS.items() if re.search(p, t)]
    if len(hits) != 1:
        return None
    if _digits(t) < 4 and not has_caller:
        return None
    return hits[0], TOPICS[hits[0]][1]


def last_agent_line(history) -> str | None:
    """The agent's last spoken line in the official session.start history, if any."""
    for h in reversed(history if isinstance(history, list) else []):
        if isinstance(h, dict) and str(h.get("role", "")).lower() in ("agent", "assistant", "상담원"):
            text = h.get("text") or h.get("content")
            if text:
                return str(text)
    return None
