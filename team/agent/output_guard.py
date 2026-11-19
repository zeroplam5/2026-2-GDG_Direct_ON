"""Output check slot (출력 검사): every sentence the model says passes here before TTS.

The pre-execution gate guards what goes INTO functions; this guards what comes OUT of the
model. First check, kept deliberately simple: numbers (prices, periods, order / phone numbers,
dates) the reply states must appear somewhere the model was given - the prompt (manual,
function rules, call context), the customer's words or a function result. A number found
nowhere is likely made up ("반품은 14일 이내…" when the manual never says 14).

Modes (cfg.output_guard): "log" = keep the sentence, record a flag in the turn metrics
(default until tuned on the official manual); "block" = replace the sentence with
phrases["unverified_fallback"]; "off" = skip. More checks can be added to OutputGuard.check.

Second check (on in "log" and "block"): a sentence already said in this turn is dropped.
gpt-5.4-mini via OpenRouter sometimes writes its whole answer twice ("A B\n\nA B", seen in the
raw stream 2026-11-19); the customer must not hear it twice.
"""
from __future__ import annotations

import re

from .gate import normalize_text

DEFAULT_FALLBACK = "정확한 내용은 확인 후 안내해 드리겠습니다."
_NUMBER = re.compile(r"\d[\d,]*")


def _key(sentence: str) -> str:
    return re.sub(r"[\s.,!?~…·\"'“”‘’]", "", sentence or "")


def numbers_in(text: str) -> list[str]:
    """Digit runs (2+ digits, commas dropped) after Korean digit words are turned into digits."""
    norm = normalize_text(text)
    out = []
    for m in _NUMBER.finditer(norm):
        d = m.group(0).replace(",", "")
        if len(d) >= 2:
            out.append(d)
    return out


class OutputGuard:
    def __init__(self, mode: str = "log", fallback: str = DEFAULT_FALLBACK):
        self.mode = mode
        self.fallback = fallback or DEFAULT_FALLBACK

    def check(self, sentence: str, sources: str, spoken=()) -> tuple[str, dict | None]:
        """Returns (sentence to speak, flag or None). An empty sentence means: say nothing."""
        if self.mode == "off" or not sentence.strip():
            return sentence, None
        key = _key(sentence)
        if key and any(_key(s) == key for s in spoken):
            return "", {"kind": "repeated_sentence", "action": "drop"}
        known = normalize_text(sources)
        missing = [n for n in numbers_in(sentence) if n not in known]
        if not missing:
            return sentence, None
        flag = {"kind": "ungrounded_number", "values": missing, "action": self.mode}
        if self.mode == "block":
            return self.fallback, flag
        return sentence, flag
