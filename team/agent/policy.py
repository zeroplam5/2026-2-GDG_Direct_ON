"""Policy helpers: system prompt assembly, session context, hand-off hints, rules file.

Every rule the code checks comes from ONE rules file (cfg.rules_path, part of a pack, see
prompts/pack.json): per-function gate rules, hand-off signal keywords, consent words.
Nothing here is an official business rule. Hand-off keywords only *flag* a turn as a
hand-off candidate (logged + hinted to the LLM); the actual hand-off mechanism and criteria
must come from the official manual (2026-11-21).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

# Fallback only, used when a rules file has no "handoff" section. The rules file is the source.
HANDOFF_PATTERNS = {
    "human_request": r"상담원|사람(이랑|과|하고|한테)|직원(이랑|과|하고|한테)|연결해\s*주",
    "health_issue": r"부작용|두드러기|가려|구토|토했|설사|어지러|호흡|숨이|응급|병원|발진|복통|이상\s*반응",
    "complaint": r"화가|짜증|어이가|신고|소비자원|고소",
}
HANDOFF_LABELS = {"human_request": "사람 상담원 요청", "health_issue": "건강 이상 호소 가능성", "complaint": "불만 표현"}


@lru_cache(maxsize=8)
def load_rules(path: str) -> dict:
    """The single rules file (practice: prompts/practice_rules.json). {} if missing."""
    p = Path(path) if path else None
    if not p or not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _handoff_table(rules: dict | None) -> dict:
    table = (rules or {}).get("handoff")
    if table:
        return {k: (v["pattern"], v.get("label", k)) for k, v in table.items()}
    return {k: (p, HANDOFF_LABELS.get(k, k)) for k, p in HANDOFF_PATTERNS.items()}


def handoff_flags(text: str, rules: dict | None = None) -> list[str]:
    return [k for k, (p, _) in _handoff_table(rules).items() if re.search(p, text or "")]


def _obj(word: str) -> str:
    """Korean object particle: 을 after a final consonant, 를 otherwise."""
    ch = word[-1:] if word else ""
    if "가" <= ch <= "힣":
        return word + ("을" if (ord(ch) - 0xAC00) % 28 else "를")
    return word + "을(를)"


def function_rules_text(rules: dict | None) -> str:
    """Prompt section generated from the same table the gate enforces, so the model's
    instructions and the code checks cannot drift apart."""
    funcs = (rules or {}).get("functions") or {}
    if not funcs:
        return ""
    labels = (rules or {}).get("arg_labels") or {}
    lines = ["[함수 실행 규칙 — 시스템이 코드로도 같은 기준을 검사함]"]
    for name, sp in funcs.items():
        args = "·".join(labels.get(a, a) for a in sp.get("value_args") or [])
        if sp.get("kind") == "read":
            how = f"{args}가 대화에 나오면 바로 호출한다." if args else "필요할 때 바로 호출한다."
            lines.append(f"- {name} (조회): {how}")
            continue
        confirm = sp.get("confirm", "explicit")
        if confirm == "explicit":
            how = (f"직전에 {_obj(args)} 복창해 진행 여부를 묻고, 고객이 이번 말에서 동의했을 때만 호출한다."
                   if args else "직전에 진행 여부를 묻고, 고객이 이번 말에서 동의했을 때만 호출한다.")
        elif confirm == "request":
            how = f"고객이 이번 말에서 {_obj(args)} 직접 말하며 요청했을 때만 호출한다." if args \
                else "고객이 이번 말에서 직접 요청했을 때만 호출한다."
        else:
            how = "고객 의사가 분명할 때 호출한다."
        lines.append(f"- {name} (접수·변경): {how}")
    default = {**{"kind": "write", "confirm": "explicit"}, **((rules or {}).get("_default") or {})}
    if default.get("kind") == "write":
        lines.append("- 위에 없는 함수: 진행 여부를 묻고 고객이 동의했을 때만 호출한다.")
    lines.append("- 동의를 받기 전에는 함수를 부르지 말고, 실행할 내용과 값을 복창해 확인 질문을 한다.")
    return "\n".join(lines)


def load_prompt(path: str) -> str:
    raw = Path(path).read_text(encoding="utf-8")
    return re.sub(r"<!--.*?-->", "", raw, flags=re.S).strip()


def load_phrases(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


_ROLE = {"user": "customer", "customer": "customer", "고객": "customer",
         "assistant": "agent", "agent": "agent", "상담원": "agent"}


def normalize_history(history) -> list[tuple[str, str]]:
    """THE single place that knows the history shape. The starter kit only shows history=[]
    and the official schema is published on 2026-11-21: adapt this function then.
    Returns [(role, text)] with role "customer" | "agent" | other label, or [] when the
    shape is unknown (callers then fall back to the raw JSON)."""
    if not isinstance(history, list) or not all(isinstance(h, dict) for h in history):
        return []
    out = []
    for h in history:
        role = h.get("role") or h.get("speaker") or "?"
        text = h.get("text") or h.get("content") or h.get("utterance")
        if text is None:
            return []
        out.append((_ROLE.get(str(role).lower(), str(role)), str(text)))
    return out


def format_history(history) -> str:
    """Known shapes are rendered as "고객: …" / "상담원: …" lines, anything else as JSON."""
    if not history:
        return ""
    turns = normalize_history(history)
    who = {"customer": "고객", "agent": "상담원"}
    lines = [f"{who.get(r, r)}: {t}" for r, t in turns]
    s = "\n".join(lines) if lines else json.dumps(history, ensure_ascii=False)
    return s[-3000:]


def build_static_prompt(template: str, phrases: dict, rules: dict | None = None, now=None) -> str:
    """The part that is the same for every call: manual / tone (template) + function rules.
    It goes first so the provider's prompt cache can reuse it across calls. A template that
    still contains {now} gets it filled here (works, but then the prefix changes per call)."""
    prompt = template.replace("{brand_name}", phrases.get("brand_name", ""))
    if "{now}" in prompt:
        prompt = prompt.replace("{now}", str(now or "알 수 없음"))
    parts = [prompt]
    fr = function_rules_text(rules)
    if fr:
        parts.append(fr)
    return "\n\n".join(parts)


def build_context(start: dict) -> str:
    """The part that changes every call: current time, caller info, earlier conversation."""
    parts = ["[상담 정보 — 이번 통화]\n현재 시각: " + str(start.get("now") or "알 수 없음")]
    caller = start.get("caller")
    if caller:
        parts.append("[발신자 정보 — 시스템 제공 참고 자료, 본인 확인 완료를 뜻하지 않음]\n"
                     + json.dumps(caller, ensure_ascii=False)[:1000])
    hist = format_history(start.get("history"))
    if hist:
        parts.append("[이전 상담 이력 — 참고 자료]\n" + hist)
    return "\n\n".join(parts)


def build_system_prompt(template: str, start: dict, phrases: dict, rules: dict | None = None) -> str:
    """Static part + per-call context as one string (older callers). The agent itself sends
    them as two system messages, static first (see Dialog)."""
    return build_static_prompt(template, phrases, rules, start.get("now")) + "\n\n" + build_context(start)


def user_message(text: str, flags: list[str], rules: dict | None = None) -> str:
    if not flags:
        return text
    labels = {k: label for k, (_, label) in _handoff_table(rules).items()}
    note = ", ".join(labels.get(f, f) for f in flags)
    return f"{text}\n\n[시스템 메모: 인계 후보 신호 감지 — {note}. 연결·접수가 완료됐다고 말하지 말 것.]"
