"""Output check slot (출력 검사): every sentence the model says passes here before TTS.

The pre-execution gate guards what goes INTO functions; this guards what comes OUT of the
model. Checks, in order:

1. Repeated sentence (always, unless "off"): a sentence already said in this turn is dropped.
   gpt-5.4-mini via OpenRouter sometimes writes its whole answer twice ("A B\\n\\nA B", seen in
   the raw stream 2026-11-19); the customer must not hear it twice.

2. Numbers found nowhere: numbers (prices, periods, order / phone numbers, dates) the reply
   states must appear somewhere the model was given - the prompt (manual, function rules, call
   context), the customer's words or a function result. A number found nowhere is likely made
   up ("반품은 14일 이내…" when the manual never says 14).
   * ID-like numbers (9+ digits such as phone numbers, or digits glued to letters such as
     DEMO1001) are BLOCKED when "ids" is in `strict`, whatever the mode: a wrong phone or
     order number read back to the customer is never harmless. Replaced with
     phrases["unverified_id_fallback"]. (Seen 2026-11-21: gpt-4.1 asked "010-1234-5678이
     맞는지" with no caller info; gpt-5.4-mini read 01000001234 back as "010 공일공공 일이삼사".)
   * other numbers follow the mode: "log" keeps the sentence and records a flag (default
     until tuned on the official manual), "block" replaces it with phrases["unverified_fallback"].

3. Unconfirmed time (when "time" is in `strict`): a sentence that says a contact / booking
   is done or promised for a time ("내일 오전 열 시쯤으로 확인해 두었습니다", "내일 오전에
   연락드리겠습니다") while no function result this session carries a time value. The callback
   function only returns {"status":"ok","message":"상담원 콜백 접수"}; the customer's wished-for
   time is not part of it. Negated sentences ("시간은 반영되지 않았습니다") pass.
   A statement is replaced with phrases["unconfirmed_time_fallback"]. (Seen 2026-11-21:
   gpt-5.4-mini said it in 2 of 4 callback answers although the prompt already forbids it.)
   A question offering the time ("내일 오전 10시에 접수해도 괜찮을까요?", gpt-4.1) is kept so
   the customer can answer, with phrases["unconfirmed_time_note"] said before it once per turn.

`strict` comes from cfg.output_guard_strict ("ids,time" by default, "" = log only).
The word patterns of check 3 come from the rules file ("output_check"), with the defaults below
as fallback - like the gate's consent words.
"""
from __future__ import annotations

import re

from .gate import QUESTION, normalize_text

DEFAULT_FALLBACK = "정확한 내용은 확인 후 안내해 드리겠습니다."
DEFAULT_ID_FALLBACK = "번호를 정확히 확인하기 위해 한 번 더 말씀해 주시겠어요?"
DEFAULT_TIME_FALLBACK = "희망하신 시간은 지금 확정해 드릴 수 없습니다."
DEFAULT_TIME_NOTE = "희망 시간은 확정되지 않았습니다."
_NUMBER = re.compile(r"\d+")
ID_MIN_DIGITS = 9  # phone numbers have 9-11 digits; prices / periods / dates in speech have fewer

# check 3 defaults (rules file "output_check" overrides)
TIME_WORDS = (r"내일|모레|글피|오늘|오전|오후|아침|저녁|점심|요일|주말|다음\s*주|\d+\s*시(?!간)|"
              r"(?<![가-힣])(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|열한|열두)\s*시(?!간)")
CLAIM_WORDS = (r"접수|예약|확정|잡아\s*(?:두|드)|해\s*두었|해\s*뒀|연락\s*드리|전화\s*드리|연락\s*드릴|전화\s*드릴|"
               r"연락드리|전화드리|연락드릴|전화드릴|콜백|전달|반영|등록|메모|기록|남겨\s*(?:두|드)")
NEGATION_WORDS = r"않|못\s*하|못\s*했|없|불가|어렵|드릴\s*수\s*없"
RESULT_TIME = r"\d{1,2}:\d{2}|\d{4}-\d{1,2}-\d{1,2}|\d+\s*시|오전|오후|내일|모레|\bAM\b|\bPM\b|T\d{2}:"


def _key(sentence: str) -> str:
    return re.sub(r"[\s.,!?~…·\"'“”‘’]", "", sentence or "")


def _numbers(text: str) -> list[tuple[str, bool]]:
    """(digit run of 2+ digits, id-like?) after Korean digit words are turned into digits."""
    norm = normalize_text(text)
    out = []
    for m in _NUMBER.finditer(norm):
        d = m.group(0)
        if len(d) >= 2:
            glued = m.start() > 0 and "A" <= norm[m.start() - 1] <= "Z"
            out.append((d, glued or len(d) >= ID_MIN_DIGITS))
    return out


def numbers_in(text: str) -> list[str]:
    """Digit runs (2+ digits, commas dropped) after Korean digit words are turned into digits."""
    return [d for d, _ in _numbers(text)]


def parse_strict(value) -> set[str]:
    if isinstance(value, (set, list, tuple)):
        return {str(v).strip() for v in value if str(v).strip()}
    return {p.strip() for p in str(value or "").split(",") if p.strip()}


class OutputGuard:
    def __init__(self, mode: str = "log", fallback: str = DEFAULT_FALLBACK, strict="", phrases: dict | None = None,
                 rules: dict | None = None):
        self.mode = mode
        self.fallback = fallback or DEFAULT_FALLBACK
        self.strict = parse_strict(strict)
        phrases = phrases or {}
        self.id_fallback = phrases.get("unverified_id_fallback") or DEFAULT_ID_FALLBACK
        self.time_fallback = phrases.get("unconfirmed_time_fallback") or DEFAULT_TIME_FALLBACK
        self.time_note = phrases.get("unconfirmed_time_note") or DEFAULT_TIME_NOTE
        words = (rules or {}).get("output_check") or {}
        self.time_words = re.compile(words.get("time") or TIME_WORDS)
        self.claim_words = re.compile(words.get("claim") or CLAIM_WORDS)
        self.negation_words = re.compile(words.get("negation") or NEGATION_WORDS)
        self.result_time = re.compile(words.get("result_time") or RESULT_TIME)
        self.question = re.compile(((rules or {}).get("consent") or {}).get("question") or QUESTION.pattern)

    def time_claim(self, sentence: str, results: str) -> tuple[str, bool] | None:
        """(time words, is a question) of a sentence that states or offers a contact / booking
        for a time no function result carries; None when the sentence is fine."""
        if not self.time_words.search(sentence) or not self.claim_words.search(sentence):
            return None
        if self.negation_words.search(sentence) or self.result_time.search(results or ""):
            return None
        words = ",".join(m.group(0) for m in self.time_words.finditer(sentence))
        return words, bool(self.question.search(sentence))

    def check(self, sentence: str, sources: str, spoken=(), results: str = "") -> tuple[str, dict | None]:
        """Returns (sentence to speak, flag or None). An empty sentence means: say nothing.
        sources = everything the model was given or told; results = function results only."""
        if self.mode == "off" or not sentence.strip():
            return sentence, None
        key = _key(sentence)
        if key and any(_key(s) == key for s in spoken):
            return "", {"kind": "repeated_sentence", "action": "drop"}
        known = normalize_text(sources)
        missing = [(n, is_id) for n, is_id in _numbers(sentence) if n not in known]
        def replace(text, flag):  # the same fallback is not said twice in one turn
            return ("", flag) if any(_key(s) == _key(text) for s in spoken) else (text, flag)

        ids = [n for n, is_id in missing if is_id]
        if ids and "ids" in self.strict:
            return replace(self.id_fallback, {"kind": "ungrounded_id", "values": ids, "action": "block"})
        if "time" in self.strict:
            found = self.time_claim(sentence, results)
            if found and not found[1]:
                return replace(self.time_fallback, {"kind": "unconfirmed_time", "values": [found[0]], "action": "block"})
            if found:  # a question offering the time: keep it (the customer must be able to answer),
                # say once per turn first that the time is not confirmed
                flag = {"kind": "unconfirmed_time_question", "values": [found[0]], "action": "note"}
                told = any(_key(self.time_note) in _key(s) or _key(self.time_fallback) in _key(s) for s in spoken)
                return (sentence if told else f"{self.time_note} {sentence}"), flag
        if not missing:
            return sentence, None
        flag = {"kind": "ungrounded_number", "values": [n for n, _ in missing], "action": self.mode}
        if self.mode == "block":
            return replace(self.fallback, flag)
        return sentence, flag
