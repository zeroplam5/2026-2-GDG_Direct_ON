"""실행 전 관문 (pre-execution gate) for function calls.

The LLM decides *what* to call; this module decides, in code, whether the call may actually
be sent to tools_url. Blocked calls are never sent (so the evaluator's relay never records
them) and the model gets a {"status": "not_executed", ...} result telling it what to ask.

Checks, in order:
  0. values      - required values are not empty ("require_any": one of a group is enough;
                   confirm="context" write functions: every value argument)
  1. format      - argument values match the per-function regex (if configured)
  2. grounding   - value arguments appear in this utterance, the session history (including
                   earlier function calls / results), the caller info or earlier turns of this
                   session (after normalising Korean digit words, "데모" -> DEMO, spaces).
                   Read functions may also use this turn's function results; write functions
                   may not ("unconfirmed_lookup": look up first, tell the customer, then act).
  3. consent     - write functions only, by the function's confirm mode:
       context  (official pack, 2026-11-21 day kit): the real call logs register a request in
                the same turn the customer gives the value or the choice, without a separate
                read-back. So: no refusal / hesitation in this utterance, not a puzzled "네?",
                and the request itself ("intent" words) appears in this utterance, an earlier
                customer turn or a recent agent turn.
       explicit / request (practice pack):
         a. no refusal / hesitation / correction words in this utterance
         b. the model quoted the customer's consent (CONFIRM_FIELD), the quote is really in
            this utterance, is affirmative and is not a puzzled "네?"
         c. explicit: the agent's previous turn asked about these exact values
            request:  the customer stated these values in this utterance
Hybrid by design: the model decides what to call and with which values, the code has the
final say with checks the model cannot fake.

The per-function table (kind / confirm / value_args / formats) and the consent / refusal word
patterns come from the single rules file (cfg.rules_path, see prompts/pack.json), so the
official function list can be filled in on 2026-11-21 without code changes. Functions missing
from the table fall back to "_default" (write + explicit confirm), i.e. the safe side.
The patterns below are fallbacks for a rules file without a "consent" section.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field

from .policy import load_rules, normalize_history

CONFIRM_FIELD = "_customer_confirmation"
CONFIRM_DESCRIPTION = ("내부 확인용 칸: 고객이 이번 말에서 이 실행에 동의한 표현을 고객 말 그대로 옮겨 적는다. "
                       "동의한 표현이 없으면 빈 문자열로 둔다.")

# Evidence field (근거 칸, cfg.gate_evidence): write functions get one more internal field where the
# model copies, for every value it put in, the customer's words the value came from. The code checks
# each quote against what the customer really said (agent words do not count), that IDs and names are
# inside their quote, that the customer did not correct the number later, and that free-text values
# (reason, pickup address, refund account) come from the customer at all. Stripped before sending.
# Official practice data (2026-11-21): all 8 reasons in the reference registrations summarise the
# customer's own words (none verbatim, none invented), mostly from earlier turns.
EVIDENCE_FIELD = "_evidence"
EVIDENCE_DESCRIPTION = ("내부 확인용 칸(고객에게 말하지 않음): 이 함수에 넣은 값마다 그 값이 나온 고객 말을 "
                        "고객이 말한 그대로 짧게 옮겨 적는다. 예: {\"order_number\": \"주문번호는 …이에요\", "
                        "\"reason\": \"먹어도 효과가 없어서요\"}. 값이 발신자 정보에서 왔으면 \"발신자 정보\", "
                        "조회 결과에서 왔으면 \"조회 결과\"라고 적는다. 고객이 말하지 않은 값은 지어내지 말고 고객에게 먼저 묻는다.")
# names the model used instead of EVIDENCE_FIELD (seen 2026-11-21: "evidence" in 2 of 13 calls, sent on)
EVIDENCE_ALIASES = ("evidence", "_근거", "근거", "_evidences")
SOURCE_WORDS = re.compile(r"^\s*(?:발신자|조회\s*결과|이전\s*조회)")
CORRECTION_WORDS = re.compile(r"아니|잘못|정정|다시\s*말|말고|그게\s*아니|틀렸")
# word stems that say nothing about WHY (a reason that only shares these with the customer is not grounded)
_STEM_STOP = {"반품", "환불", "교환", "취소", "요청", "신청", "접수", "고객", "주문", "상품", "제품", "싶어", "싶습",
              "합니", "해주", "해드", "부탁", "원함", "원하", "처리", "전체", "일부", "하고", "위해", "때문", "으로",
              "그냥", "모두", "하나", "개를", "있습", "없습"}

_KO_DIGITS = {"공": "0", "영": "0", "일": "1", "이": "2", "삼": "3", "사": "4", "오": "5",
              "육": "6", "륙": "6", "칠": "7", "팔": "8", "구": "9"}
_KO_RUN = re.compile(r"(?:[공영일이삼사오육륙칠팔구]\s*){3,}")  # 3+ digit words in a row ("이에요" stays)

NEGATIVE = re.compile(r"아니|아뇨|잠깐|잠시만|생각(?:해|좀|을|해\s*볼)|나중에|보류|말고|됐어요|됐습니다|필요\s*없|하지\s*마")
AFFIRMATIVE = re.compile(r"^\s*(?:네|예|응|맞|그래|그럼|좋)|해\s*주세요|해\s*줘|해\s*주시|부탁|진행|접수해|그렇게|그걸로|좋아요|좋습니다|맞아요|맞습니다")
PUZZLED = re.compile(r"^\s*(?:네|예|뭐|뭐라고|네에)\s*요?\s*\?\s*$")
QUESTION = re.compile(r"\?|까요|나요|가요|실래요|시겠어요|시겠습니까|맞으세요|맞습니까|맞나요|될까요|드릴까요")

MESSAGES = {
    "invalid_arguments": "실행하지 않았습니다: 함수 인자를 읽을 수 없습니다. 필요한 정보를 고객에게 다시 확인하세요.",
    "invalid_format": "실행하지 않았습니다: {arg} 값({value})의 형식이 맞지 않습니다. 고객에게 그 정보를 한 번 더 확인하세요.",
    "not_grounded": "실행하지 않았습니다: {arg} 값({value})이 대화에 나온 적이 없습니다. 추측하지 말고 고객에게 물어보세요.",
    "hesitation": ("실행하지 않았습니다: 고객이 거절·보류·정정 의사를 보였습니다. 정정한 값이 있으면 그 값을 복창해 다시 확인하고, "
                   "아니면 재촉하지 말고 고객 말에 맞게 답하세요."),
    "puzzled": "실행하지 않았습니다: 고객이 질문을 다시 묻고 있습니다. 직전 질문을 짧고 분명하게 다시 물으세요.",
    "no_consent": ("실행하지 않았습니다: 이번 고객 말에서 실행 동의를 찾지 못했습니다. "
                   "실행할 내용과 값({values})을 한 번 복창하고 진행해도 되는지 한 번만 물으세요."),
    "not_asked": ("실행하지 않았습니다: 직전 상담원 질문에서 이 값({values})으로 진행할지 묻지 않았습니다. "
                  "값을 복창하고 진행해도 되는지 한 번만 물으세요."),
    "not_stated": ("실행하지 않았습니다: 고객이 이번 말에서 이 값({values})을 직접 말하지 않았습니다. "
                   "값을 복창하고 진행해도 되는지 한 번만 물으세요."),
    "missing_value": "실행하지 않았습니다: 필요한 값({arg})이 없습니다. 추측하지 말고 고객에게 물어보세요.",
    "unconfirmed_lookup": ("실행하지 않았습니다: {arg} 값({value})은 이번에 처음 조회한 결과에만 있습니다. "
                           "바로 접수하지 말고 조회 결과를 안내한 뒤 어느 건을 진행할지 고객에게 확인하세요."),
    "no_intent": ("실행하지 않았습니다: 고객이 이 처리를 원한다는 말이 대화에 없습니다. "
                  "고객이 원하는 것을 먼저 확인하세요."),
    "no_evidence": ("실행하지 않았습니다: {label} 값의 근거가 되는 고객 말을 찾지 못했습니다. "
                    "지어내지 말고 고객에게 {label}을(를) 여쭤보세요."),
    "evidence_mismatch": ("실행하지 않았습니다: {label} 값({value})이 근거로 적은 고객 말에 없습니다. "
                          "고객이 말한 값을 그대로 쓰고, 분명하지 않으면 한 번 더 여쭤보세요."),
    "corrected": ("실행하지 않았습니다: 고객이 {label} 값({value})을 뒤에서 다른 값으로 정정했습니다. "
                  "정정한 값을 쓰세요."),
}

# agent turns that are system notices, not questions ("…상담이 자동으로 종료됩니다")
NOTICE = re.compile(r"자동으로\s*종료|자동\s*종료|상담이\s*종료")
# argument values that stand for "unknown" (seen 2026-11-21: customer_name="고객님", which the
# agent's "안녕하세요, 고객님!" made look grounded; phone_number="발신자 정보 없음"). Treated as empty.
PLACEHOLDER = re.compile(r"^(?:고객|손님|회원|본인|담당자|구매자|주문자)\s*님?$|^(?:없음|미상|모름|알\s*수\s*없음|unknown|none|null|n/?a)$"
                         r"|정보\s*없음|확인\s*필요|접수\s*불가", re.I)
RECENT_AGENT_TURNS = 3  # how many recent agent turns may carry the request (context mode)


def _values(args: dict, path: str) -> list:
    """Non-empty scalar values at `path` ("a" or "a.b"; a list on the way is walked)."""
    cur = [args]
    for part in path.split("."):
        nxt = []
        for c in cur:
            v = c.get(part) if isinstance(c, dict) else None
            if isinstance(v, list):
                nxt.extend(v)
            elif v is not None:
                nxt.append(v)
        cur = nxt
    return [v for v in cur if not isinstance(v, (dict, list)) and str(v).strip()]


def normalize_text(text: str) -> str:
    """Comparable form: Korean digit words -> digits, 데모 -> DEMO, upper case, alphanumerics only."""
    s = str(text or "")
    s = s.replace("데모", "DEMO")
    s = _KO_RUN.sub(lambda m: "".join(_KO_DIGITS[c] for c in m.group(0) if c in _KO_DIGITS), s)
    return re.sub(r"[^0-9A-Za-z가-힣]", "", s).upper()


def normalize_value(value) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", str(value or "")).upper()


def _compact(text: str) -> str:
    return re.sub(r"[\s.,!?~…·\"'“”‘’]", "", str(text or ""))


def _stems(text: str) -> set[str]:
    """First two letters of each Korean word, minus stems that say nothing about the content."""
    return {w[:2] for w in re.findall(r"[가-힣]{2,}", str(text or ""))} - _STEM_STOP


def _shares_word(value: str, text: str) -> bool:
    return any(s in str(text or "") for s in _stems(value))


def _find_quote(quote: str, text: str) -> int | None:
    """Position (in the compact text) where the customer said `quote`. A long quote may be cut or
    joined differently: then its longest piece of 6+ letters must be found."""
    q, t = _compact(quote), _compact(text)
    if not q:
        return None
    i = t.find(q)
    if i >= 0:
        return i + len(q)
    pieces = sorted((p for p in re.split(r"[\s.,!?~…·\"'“”‘’]+", quote) if len(_compact(p)) >= 6),
                    key=len, reverse=True)
    for p in pieces[:1]:
        i = t.find(_compact(p))
        if i >= 0:
            return i + len(_compact(p))
    return None


def _corrected_later(value: str, text: str) -> bool:
    """The customer said `value` (9+ digits) and later, after a correction word ("아 잘못 말했다"),
    another number of the same length. Two numbers without a correction word (two orders) are fine."""
    s = _KO_RUN.sub(lambda m: "".join(_KO_DIGITS.get(c, c) for c in m.group(0) if not c.isspace()), str(text or ""))
    nums = [(m.start(), m.end(), re.sub(r"\D", "", m.group(0)))
            for m in re.finditer(r"\d(?:[\d\-.]|\s(?=\d))*\d", s)]
    mine = [n for n in nums if n[2] == value]
    if not mine:
        return False
    end = mine[-1][1]
    for start, _, d in nums:
        if start >= end and len(d) == len(value) and d != value and CORRECTION_WORDS.search(s[end:start + 1]):
            return True
    return False


@dataclass
class Decision:
    allowed: bool
    args: dict = field(default_factory=dict)    # arguments to send (internal field removed)
    reason: str | None = None
    message: str = ""
    evidence: dict | None = None  # argument -> ok / source / fallback / missing (no payloads)

    def result(self) -> dict:
        """What the model sees instead of a function result when the call is blocked."""
        return {"status": "not_executed", "reason": self.reason, "message": self.message}

    def log_entry(self, name: str) -> dict:
        e = {"name": name, "allowed": self.allowed, "reason": self.reason}  # no payloads
        if self.evidence is not None:
            e["evidence"] = self.evidence
        return e


class Gate:
    DEFAULT_SPEC = {"kind": "write", "confirm": "explicit"}

    def __init__(self, rules: dict | None = None, start: dict | None = None, evidence: bool = False):
        self.table = rules or {}
        self.evidence = evidence  # 근거 칸 (EVIDENCE_FIELD) on write functions
        start = start or {}
        self.history = normalize_history(start.get("history"))
        caller = start.get("caller")
        self.caller_text = json.dumps(caller, ensure_ascii=False) if caller else ""
        self.schemas: dict[str, dict] = {}
        words = self.table.get("consent") or {}
        self.placeholder = re.compile(self.table["placeholder"], re.I) if self.table.get("placeholder") else PLACEHOLDER
        self.negative = re.compile(words["negative"]) if words.get("negative") else NEGATIVE
        self.affirmative = re.compile(words["affirmative"]) if words.get("affirmative") else AFFIRMATIVE
        self.puzzled = re.compile(words["puzzled"]) if words.get("puzzled") else PUZZLED
        self.question = re.compile(words["question"]) if words.get("question") else QUESTION

    @classmethod
    def from_config(cls, cfg, start: dict) -> "Gate":
        return cls(load_rules(cfg.rules_path), start, evidence=getattr(cfg, "gate_evidence", False))

    # ── schema side ───────────────────────────────────────────────────────
    def spec(self, name: str) -> dict:
        functions = self.table.get("functions") or {}
        base = {**self.DEFAULT_SPEC, **(self.table.get("_default") or {})}
        return {**base, **functions[name]} if name in functions else base

    def needs_consent(self, name: str) -> bool:
        """Write functions whose consent is a quote the model copies into CONFIRM_FIELD
        (explicit / request). "context" checks the conversation itself and adds no field."""
        sp = self.spec(name)
        return sp.get("kind") == "write" and sp.get("confirm", "explicit") in ("explicit", "request")

    def augment(self, schemas: list[dict]) -> list[dict]:
        """Add the internal consent field to write functions. Input schemas are not modified."""
        out = []
        for s in schemas or []:
            f = s.get("function", s)
            name = f.get("name", "")
            self.schemas[name] = f
            consent = self.needs_consent(name)
            evidence = self.evidence and self.spec(name).get("kind") == "write"
            if not consent and not evidence:
                out.append(s)
                continue
            s2 = copy.deepcopy(s)
            f2 = s2.get("function", s2)
            params = f2.setdefault("parameters", {"type": "object", "properties": {}})
            props = params.setdefault("properties", {})
            req = list(params.get("required") or [])
            if consent:
                props[CONFIRM_FIELD] = {"type": "string", "description": CONFIRM_DESCRIPTION}
                req += [] if CONFIRM_FIELD in req else [CONFIRM_FIELD]
            if evidence:
                props[EVIDENCE_FIELD] = {"type": "object", "description": EVIDENCE_DESCRIPTION,
                                         "additionalProperties": {"type": "string"}}
                req += [] if EVIDENCE_FIELD in req else [EVIDENCE_FIELD]
            params["required"] = req
            out.append(s2)
        return out

    def _label(self, arg: str) -> str:
        labels = self.table.get("arg_labels") or {}
        return labels.get(arg) or {"reason": "사유", "pickup_address": "회수지 주소",
                                    "refund_account": "환불 계좌"}.get(arg, arg)

    def check_evidence(self, args: dict, evidence, value_args: list[str], customer: str):
        """(blocked argument, block reason) or (None, {argument: status}). Top-level string values only.
        ID-like values (value_args, digits) passed the grounding check already: a missing or unfound
        quote is logged, a value outside its quote or a later correction blocks. Free-text values
        (reason, address, account) must come from the customer: quote found, or (no usable quote)
        the value shares a content word with what the customer said."""
        ev = evidence if isinstance(evidence, dict) else ({"*": str(evidence)} if evidence else {})
        id_args = {a.split(".")[0] for a in value_args}
        status = {}
        for a, v in args.items():
            if a in (CONFIRM_FIELD, EVIDENCE_FIELD, *EVIDENCE_ALIASES) or not isinstance(v, str) or not v.strip() \
                    or self.placeholder.search(v.strip()):
                continue
            q = str(ev.get(a) or "").strip()
            free = a not in id_args and not re.fullmatch(r"[\d\-\s]+", v)
            nv = normalize_value(v)
            if not free and re.fullmatch(r"\d{9,}", nv) and _corrected_later(nv, customer):
                return a, "corrected"
            if q and SOURCE_WORDS.match(q):
                if free:  # a reason does not come from the caller record or a lookup
                    return a, "no_evidence"
                status[a] = "source"
                continue
            pos = _find_quote(q, customer) if q else None
            if free:
                if pos is not None:
                    status[a] = "ok"
                elif _shares_word(v, customer):
                    status[a] = "fallback"
                else:
                    return a, "no_evidence"
                continue
            if pos is None:
                status[a] = "missing" if not q else "quote_not_found"
                continue
            if nv not in normalize_text(q) and nv not in normalize_value(q):
                return a, "evidence_mismatch"
            status[a] = "ok"
        return None, status

    # ── decision side ─────────────────────────────────────────────────────
    def last_agent_text(self) -> str:
        for role, text in reversed(self.history):
            if role == "agent":
                return text
        return ""

    def recent_agent_texts(self, n: int = RECENT_AGENT_TURNS) -> list[str]:
        """The last n agent turns of the session history, system notices skipped."""
        out = []
        for role, text in reversed(self.history):
            if role == "agent" and not NOTICE.search(text):
                out.append(text)
                if len(out) >= n:
                    break
        return out

    def _intent_text(self, utterance: str, prior: str) -> str:
        """Where a request can come from: this utterance, every earlier customer turn, the recent
        agent turns (the agent asking "전체 반품할까요, 일부만 할까요?") and earlier turns of this session."""
        customer = [t for r, t in self.history if r == "customer"]
        return "\n".join([utterance or "", *customer, *self.recent_agent_texts(), prior or ""])

    def _value_args(self, name: str, sp: dict) -> list[str]:
        if "value_args" in sp:
            return list(sp["value_args"])
        return list((self.schemas.get(name, {}).get("parameters") or {}).get("required") or [])

    def check(self, name: str, arguments_json: str, utterance: str, last_agent: str | None = None,
              prior: str = "", turn_results: str = "", customer: str | None = None) -> Decision:
        """prior = earlier turns of this session (live multi-turn use; the evaluator sends one
        utterance per connection, so it is empty there). turn_results = function results of
        this turn so far. customer = everything the customer said, in order (session.start history
        + this session); default: history customer lines + this utterance."""
        self._ev_status = None
        d = self._check(name, arguments_json, utterance, last_agent, prior, turn_results, customer)
        if d.allowed and self._ev_status is not None:
            d.evidence = self._ev_status
        return d

    def _check(self, name, arguments_json, utterance, last_agent, prior, turn_results, customer) -> Decision:
        try:
            args = json.loads(arguments_json or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            return Decision(False, reason="invalid_arguments", message=MESSAGES["invalid_arguments"])
        quote = str(args.pop(CONFIRM_FIELD, "") or "").strip()
        evidence = args.pop(EVIDENCE_FIELD, None)
        for alias in EVIDENCE_ALIASES:  # the model sometimes drops the underscore: never send it on
            if alias in args:
                v = args.pop(alias)
                evidence = evidence if evidence is not None else v
        sp = self.spec(name)
        mode = sp.get("confirm", "explicit")
        is_write = sp.get("kind") == "write"

        def real(a):  # values of argument a, placeholders such as "고객님" / "없음" left out
            return [v for v in _values(args, a) if not self.placeholder.search(str(v).strip())]

        all_value_args = self._value_args(name, sp)
        value_args = [a for a in all_value_args if real(a)]
        values_txt = ", ".join(f"{a}={'·'.join(map(str, real(a)))}" for a in value_args) or "요청 내용"

        def block(reason, **kw):
            return Decision(False, args, reason, MESSAGES[reason].format(values=values_txt, **kw))

        # 0. required values
        group = sp.get("require_any") or []
        if group and not any(real(a) for a in group):
            return block("missing_value", arg=" 또는 ".join(group))
        if is_write and mode == "context":
            for a in all_value_args:
                if not real(a):
                    return block("missing_value", arg=a)
        # 1. format (empty / null / placeholder values are not checked here)
        for arg, pattern in (sp.get("formats") or {}).items():
            for v in real(arg):
                if not re.fullmatch(pattern, normalize_value(v)):
                    return block("invalid_format", arg=arg, value=v)
        # 2. grounding: in both normalisations ("공일공" -> 010, and names kept as written)
        base = "\n".join([utterance or "", " ".join(t for _, t in self.history), self.caller_text, prior or ""])
        known = (normalize_text(base), normalize_value(base))
        fresh = (normalize_text(turn_results), normalize_value(turn_results)) if turn_results else ("", "")
        for arg in value_args:
            for v in real(arg):
                nv = normalize_value(v)
                if not nv or nv in known[0] or nv in known[1]:
                    continue
                if nv in fresh[0] or nv in fresh[1]:
                    if not is_write:
                        continue  # a lookup may chain on this turn's results
                    return block("unconfirmed_lookup", arg=arg, value=v)
                return block("not_grounded", arg=arg, value=v)
        ev_status = None
        if is_write and self.evidence:
            said = customer if customer is not None else "\n".join(
                [*(t for r, t in self.history if r == "customer"), utterance or ""])
            bad, ev_status = self.check_evidence(args, evidence, all_value_args, said)
            if bad:
                return block(ev_status, label=self._label(bad), value=args.get(bad))
            self._ev_status = ev_status
        if is_write and mode == "context":
            if self.negative.search(utterance or ""):
                return block("hesitation")
            if self.puzzled.match(utterance or ""):
                return block("puzzled")
            intent = sp.get("intent")
            if intent and not re.search(intent, self._intent_text(utterance, prior)):
                return block("no_intent")
            return Decision(True, args)
        if not self.needs_consent(name):
            return Decision(True, args)
        # 3. consent (explicit / request)
        if self.negative.search(utterance or ""):
            return block("hesitation")
        if self.puzzled.match(utterance or "") or (quote and self.puzzled.match(quote)):
            return block("puzzled")
        if not quote or _compact(quote) not in _compact(utterance) or not self.affirmative.search(quote):
            return block("no_consent")
        vals = [normalize_value(v) for a in value_args for v in real(a)]
        if mode == "request":
            said = normalize_text(utterance)
            if any(v not in said for v in vals):
                return block("not_stated")
            return Decision(True, args)
        agent = self.last_agent_text() if last_agent is None else last_agent
        agent_norm = normalize_text(agent)
        if not agent or not self.question.search(agent) or any(v not in agent_norm for v in vals):
            return block("not_asked")
        return Decision(True, args)
