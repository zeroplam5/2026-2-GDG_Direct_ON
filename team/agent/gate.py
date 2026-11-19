"""실행 전 관문 (pre-execution gate) for function calls.

The LLM decides *what* to call; this module decides, in code, whether the call may actually
be sent to tools_url. Blocked calls are never sent (so the evaluator's relay never records
them) and the model gets a {"status": "not_executed", ...} result telling it what to ask.

Checks, in order:
  1. format      - argument values match the per-function regex (if configured)
  2. grounding   - value arguments appear in this utterance, the session history or the
                   caller info (after normalising Korean digit words, "데모" -> DEMO, spaces)
  3. consent     - write functions only (confirm = "explicit" | "request"):
       a. no refusal / hesitation / correction words in this utterance
       b. the model quoted the customer's consent (CONFIRM_FIELD), the quote is really in
          this utterance, is affirmative and is not a puzzled "네?"
       c. explicit: the agent's previous turn asked about these exact values
          request:  the customer stated these values in this utterance
Hybrid by design: the model handles phrasing variety (it picks the consent quote), the code
has the final say with checks the model cannot fake.

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
}


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


@dataclass
class Decision:
    allowed: bool
    args: dict = field(default_factory=dict)    # arguments to send (internal field removed)
    reason: str | None = None
    message: str = ""

    def result(self) -> dict:
        """What the model sees instead of a function result when the call is blocked."""
        return {"status": "not_executed", "reason": self.reason, "message": self.message}

    def log_entry(self, name: str) -> dict:
        return {"name": name, "allowed": self.allowed, "reason": self.reason}  # no payloads


class Gate:
    DEFAULT_SPEC = {"kind": "write", "confirm": "explicit"}

    def __init__(self, rules: dict | None = None, start: dict | None = None):
        self.table = rules or {}
        start = start or {}
        self.history = normalize_history(start.get("history"))
        caller = start.get("caller")
        self.caller_text = json.dumps(caller, ensure_ascii=False) if caller else ""
        self.schemas: dict[str, dict] = {}
        words = self.table.get("consent") or {}
        self.negative = re.compile(words["negative"]) if words.get("negative") else NEGATIVE
        self.affirmative = re.compile(words["affirmative"]) if words.get("affirmative") else AFFIRMATIVE
        self.puzzled = re.compile(words["puzzled"]) if words.get("puzzled") else PUZZLED
        self.question = re.compile(words["question"]) if words.get("question") else QUESTION

    @classmethod
    def from_config(cls, cfg, start: dict) -> "Gate":
        return cls(load_rules(cfg.rules_path), start)

    # ── schema side ───────────────────────────────────────────────────────
    def spec(self, name: str) -> dict:
        functions = self.table.get("functions") or {}
        base = {**self.DEFAULT_SPEC, **(self.table.get("_default") or {})}
        return {**base, **functions[name]} if name in functions else base

    def needs_consent(self, name: str) -> bool:
        sp = self.spec(name)
        return sp.get("kind") == "write" and sp.get("confirm", "explicit") != "none"

    def augment(self, schemas: list[dict]) -> list[dict]:
        """Add the internal consent field to write functions. Input schemas are not modified."""
        out = []
        for s in schemas or []:
            f = s.get("function", s)
            self.schemas[f.get("name", "")] = f
            if not self.needs_consent(f.get("name", "")):
                out.append(s)
                continue
            s2 = copy.deepcopy(s)
            f2 = s2.get("function", s2)
            params = f2.setdefault("parameters", {"type": "object", "properties": {}})
            params.setdefault("properties", {})[CONFIRM_FIELD] = {"type": "string", "description": CONFIRM_DESCRIPTION}
            req = list(params.get("required") or [])
            if CONFIRM_FIELD not in req:
                req.append(CONFIRM_FIELD)
            params["required"] = req
            out.append(s2)
        return out

    # ── decision side ─────────────────────────────────────────────────────
    def last_agent_text(self) -> str:
        for role, text in reversed(self.history):
            if role == "agent":
                return text
        return ""

    def _value_args(self, name: str, sp: dict) -> list[str]:
        if "value_args" in sp:
            return list(sp["value_args"])
        return list((self.schemas.get(name, {}).get("parameters") or {}).get("required") or [])

    def check(self, name: str, arguments_json: str, utterance: str, last_agent: str | None = None) -> Decision:
        try:
            args = json.loads(arguments_json or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            return Decision(False, reason="invalid_arguments", message=MESSAGES["invalid_arguments"])
        quote = str(args.pop(CONFIRM_FIELD, "") or "").strip()
        sp = self.spec(name)
        value_args = [a for a in self._value_args(name, sp) if a in args]
        values_txt = ", ".join(f"{a}={args[a]}" for a in value_args) or "요청 내용"

        def block(reason, **kw):
            return Decision(False, args, reason, MESSAGES[reason].format(values=values_txt, **kw))

        # 1. format
        for arg, pattern in (sp.get("formats") or {}).items():
            if arg in args and not re.fullmatch(pattern, normalize_value(args[arg])):
                return block("invalid_format", arg=arg, value=args[arg])
        # 2. grounding
        sources = normalize_text(utterance) + "|" + normalize_text(" ".join(t for _, t in self.history)) \
            + "|" + normalize_text(self.caller_text)
        for arg in value_args:
            v = normalize_value(args[arg])
            if v and v not in sources:
                return block("not_grounded", arg=arg, value=args[arg])
        if not self.needs_consent(name):
            return Decision(True, args)
        # 3. consent
        if self.negative.search(utterance or ""):
            return block("hesitation")
        if self.puzzled.match(utterance or "") or (quote and self.puzzled.match(quote)):
            return block("puzzled")
        if not quote or _compact(quote) not in _compact(utterance) or not self.affirmative.search(quote):
            return block("no_consent")
        mode = sp.get("confirm", "explicit")
        if mode == "request":
            said = normalize_text(utterance)
            if any(normalize_value(args[a]) not in said for a in value_args):
                return block("not_stated")
            return Decision(True, args)
        agent = self.last_agent_text() if last_agent is None else last_agent
        agent_norm = normalize_text(agent)
        if not agent or not self.question.search(agent) or any(normalize_value(args[a]) not in agent_norm for a in value_args):
            return block("not_asked")
        return Decision(True, args)
