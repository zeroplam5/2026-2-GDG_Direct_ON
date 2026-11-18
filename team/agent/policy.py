"""Practice-only policy helpers: system prompt assembly, session context, hand-off hints.

Nothing here is an official business rule. Hand-off keywords only *flag* a turn as a
hand-off candidate (logged + hinted to the LLM); the actual hand-off mechanism and criteria
must come from the official manual (2026-11-21).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HANDOFF_PATTERNS = {
    "human_request": r"상담원|사람(이랑|과|하고|한테)|직원(이랑|과|하고|한테)|연결해\s*주",
    "health_issue": r"부작용|두드러기|가려|구토|토했|설사|어지러|호흡|숨이|응급|병원|발진|복통|이상\s*반응",
    "complaint": r"화가|짜증|어이가|신고|소비자원|고소",
}


def handoff_flags(text: str) -> list[str]:
    return [k for k, p in HANDOFF_PATTERNS.items() if re.search(p, text or "")]


def load_prompt(path: str) -> str:
    raw = Path(path).read_text(encoding="utf-8")
    return re.sub(r"<!--.*?-->", "", raw, flags=re.S).strip()


def load_phrases(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def format_history(history) -> str:
    """The starter kit only shows history=[]; its official schema is not published.
    Known shapes ({role, text|content}) are rendered as lines, anything else as JSON."""
    if not history:
        return ""
    lines = []
    if isinstance(history, list) and all(isinstance(h, dict) for h in history):
        for h in history:
            role = h.get("role") or h.get("speaker") or "?"
            text = h.get("text") or h.get("content") or h.get("utterance")
            if text is None:
                lines = []
                break
            who = {"user": "고객", "customer": "고객", "assistant": "상담원", "agent": "상담원"}.get(str(role).lower(), str(role))
            lines.append(f"{who}: {text}")
    s = "\n".join(lines) if lines else json.dumps(history, ensure_ascii=False)
    return s[-3000:]


def build_system_prompt(template: str, start: dict, phrases: dict) -> str:
    prompt = template.replace("{now}", str(start.get("now") or "알 수 없음")) \
                     .replace("{brand_name}", phrases.get("brand_name", ""))
    parts = [prompt]
    caller = start.get("caller")
    if caller:
        parts.append("[발신자 정보 — 시스템 제공 참고 자료, 본인 확인 완료를 뜻하지 않음]\n"
                     + json.dumps(caller, ensure_ascii=False)[:1000])
    hist = format_history(start.get("history"))
    if hist:
        parts.append("[이전 상담 이력 — 참고 자료]\n" + hist)
    return "\n\n".join(parts)


def user_message(text: str, flags: list[str]) -> str:
    if not flags:
        return text
    labels = {"human_request": "사람 상담원 요청", "health_issue": "건강 이상 호소 가능성", "complaint": "불만 표현"}
    note = ", ".join(labels.get(f, f) for f in flags)
    return f"{text}\n\n[시스템 메모: 인계 후보 신호 감지 — {note}. 연결·접수가 완료됐다고 말하지 말 것.]"
