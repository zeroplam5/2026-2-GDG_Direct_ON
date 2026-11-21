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

from .gate import _KO_DIGITS, _KO_RUN, QUESTION, normalize_text

DEFAULT_FALLBACK = "정확한 내용은 확인 후 안내해 드리겠습니다."
DEFAULT_ID_FALLBACK = "번호를 정확히 확인하기 위해 한 번 더 말씀해 주시겠어요?"
DEFAULT_TIME_FALLBACK = "희망하신 시간은 지금 확정해 드릴 수 없습니다."
DEFAULT_TIME_NOTE = "희망 시간은 확정되지 않았습니다."
# One number as written or spoken: digit groups joined by - . , ("20990101-0001234", "12,300") or
# by one space before a 2-4 digit group ("010 9999 0000"). Other separators ("1·2·4박스",
# " - 2026-09-29") end the number, so separate numbers are never glued into one long "ID".
_NUMBER = re.compile(r"\d+(?:(?:[-.,]|\s(?=\d{2,4}(?!\d)))\d+)*")
ID_MIN_DIGITS = 9  # phone numbers have 9-11 digits; prices / periods / dates in speech have fewer
RELATIVE_DAY = r"오늘|내일|모레|글피|요일|주말|다음\s*주"
# a function call written as text instead of being called ('search_products({"query":"…"})', seen
# 2026-11-21 with gpt-5.4-mini): never read code aloud
CODE_CALL = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\s*\(\s*\{.*?\}\s*\)", re.S)

# check 3 defaults (rules file "output_check" overrides)
TIME_WORDS = (r"내일|모레|글피|오늘|오전|오후|아침|저녁|점심|요일|주말|다음\s*주|\d+\s*시(?!간)|"
              r"(?<![가-힣])(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|열한|열두)\s*시(?!간)")
CLAIM_WORDS = (r"접수|예약|확정|잡아\s*(?:두|드)|해\s*두었|해\s*뒀|연락\s*드리|전화\s*드리|연락\s*드릴|전화\s*드릴|"
               r"연락드리|전화드리|연락드릴|전화드릴|콜백|전달|반영|등록|메모|기록|남겨\s*(?:두|드)")
NEGATION_WORDS = r"않|못\s*하|못\s*했|없|불가|어렵|드릴\s*수\s*없"
RESULT_TIME = r"\d{1,2}:\d{2}|\d{4}-\d{1,2}-\d{1,2}|\d+\s*시|오전|오후|내일|모레|\bAM\b|\bPM\b|T\d{2}:"


def _key(sentence: str) -> str:
    return re.sub(r"[\s.,!?~…·\"'“”‘’]", "", sentence or "")


def _spoken_digits(text: str) -> str:
    """Korean digit words -> digits, spaces kept ("공일공 구구구구" -> "010 9999"), 데모 -> DEMO."""
    s = str(text or "").replace("데모", "DEMO")
    return _KO_RUN.sub(lambda m: "".join(_KO_DIGITS.get(c, c) for c in m.group(0)), s)


def _numbers(text: str) -> list[tuple[str, bool]]:
    """(digits of each number with 2+ digits, id-like?). ID-like = 9+ digits (phone / order
    numbers) or glued to Latin letters (DEMO1001, "DEMO 1001")."""
    s = _spoken_digits(text)
    out = []
    for m in _NUMBER.finditer(s):
        d = re.sub(r"\D", "", m.group(0))
        if len(d) >= 2:
            glued = bool(re.search(r"[A-Za-z]\s?$", s[max(0, m.start() - 2):m.start()]))
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
        self.relative_day = re.compile(words.get("relative_day") or RELATIVE_DAY)
        # optional: sentences that state the brand's general contact policy ("업무 시간인 오전 10시부터
        # … 순서대로 연락드리고") are not a time booked for this customer (official EX07)
        self.policy_words = re.compile(words["policy"]) if words.get("policy") else None
        self.question = re.compile(((rules or {}).get("consent") or {}).get("question") or QUESTION.pattern)

    def time_claim(self, sentence: str, results: str, policy: str = "", customer: str | None = None) -> tuple[str, bool] | None:
        """(time words, is a question) of a sentence that states or offers a contact / booking
        for a time no function result carries; None when the sentence is fine.
        * customer (the customer's own words, when given): only a time the CUSTOMER asked for is
          checked ("내일 오전 열 시쯤으로 부탁드려요" -> "그 시간으로 접수했습니다"). The company's own
          contact practice ("업무 시간 안에 순서대로 연락드립니다", as in the official example
          answer) is not a promise of the customer's time.
        * policy: a time the brand material itself states is fine (relative days never count)."""
        if not self.time_words.search(sentence) or not self.claim_words.search(sentence):
            return None
        if self.negation_words.search(sentence) or self.result_time.search(results or ""):
            return None
        if self.policy_words and self.policy_words.search(sentence):
            return None
        tokens = [m.group(0) for m in self.time_words.finditer(sentence)]
        if customer is not None:
            asked = _key(customer)
            if not any(_key(t) in asked for t in tokens):
                return None
        if policy and not any(self.relative_day.search(t) for t in tokens):
            stated = _key(policy)
            if all(_key(t) in stated for t in tokens):
                return None
        return ",".join(tokens), bool(self.question.search(sentence))

    def check(self, sentence: str, sources: str, spoken=(), results: str = "", policy: str = "",
              customer: str | None = None, earlier=()) -> tuple[str, dict | None]:
        """Returns (sentence to speak, flag or None). An empty sentence means: say nothing.
        sources = everything the model was given or told; results = function results only;
        policy = our instructions + brand material (what the brand itself states);
        customer = the customer's own words (history + this session), None = not known;
        earlier = sentences of the agent's previous reply (empty when the customer asked to hear it again)."""
        if self.mode == "off" or not sentence.strip():
            return sentence, None
        # the same sentence as in the previous reply (seen 2026-11-21: a courier note said again word
        # for word): dropped, but only after something was said this turn, so the turn is never silent
        key0 = _key(sentence)
        if spoken and len(key0) >= 12 and any(_key(s) == key0 for s in earlier):
            return "", {"kind": "repeated_from_last_turn", "action": "drop"}
        if CODE_CALL.search(sentence):  # strip the code, check what is left like any sentence
            rest = re.sub(r"\s+", " ", CODE_CALL.sub(" ", sentence)).strip()
            code_flag = {"kind": "code_text", "action": "strip"}
            if not rest:
                return "", code_flag
            out, flag = self.check(rest, sources, spoken, results, policy=policy, customer=customer)
            return out, flag or code_flag
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
            found = self.time_claim(sentence, results, policy, customer)
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
