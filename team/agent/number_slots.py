"""Number slots: keep phone / order numbers readable for the model behind the KULLM gateway.

Measured on 2026-11-21 (made-up values): the KULLM gateway masks personal data in USER-role
messages before the model sees them ("010-9999-0000" -> "[PHONE]", "주문번호 20990101-0001234" ->
"주문번호 [DATE-OF-BIRTH]-0001234"), so a callback was registered with phone_number "". System,
assistant and tool messages pass unchanged.

So, only on the way to the provider, ID-like numbers in user messages are replaced by slot names
("[번호1]") and the real values are listed in a table at the end of the system message. Slot names
the model writes back (text or function arguments) are replaced by the real values. Dialog history,
the gate and the output check always work on the real values.

Numbers with 9+ digits are slotted (phones, order numbers 15, item codes 17, card / account /
waybill numbers). Dates, times, prices and short numbers stay as they are (not masked, and the
model needs them as written).
"""
from __future__ import annotations

import re

# digit groups joined by single separators (-, ., space), optional leading "+"
_RUN = re.compile(r"(?<!\d)\+?\d(?:\d|[-. ](?=\d))*\d")
_TOKEN = re.compile(r"\+?\d+(?:[-.]\d+)*")  # one space-free piece of a run
_DATE = re.compile(r"\d{4}[-.]\d{1,2}[-.]\d{1,2}(?!\d)")
_SLOT = re.compile(r"\[번호(\d+)\]")
MIN_DIGITS = 9

TABLE_HEAD = ("[번호 자리표] 고객 말 속 번호는 전송 중에 가려지지 않도록 자리 이름으로 바꿔 보냈다. "
              "실제 값은 아래와 같다. 함수 인자와 답에는 실제 값을 쓴다.")


def _is_id(raw: str) -> bool:
    return len(re.sub(r"\D", "", raw)) >= MIN_DIGITS and not _DATE.match(raw.lstrip("+"))


def _groups(s: str) -> list[str]:
    return re.split(r"[-. ]", s.lstrip("+"))


def spans(text: str) -> list[tuple[int, int, str]]:
    """Numbers as written, with their position. A space joins two pieces only when both groups next
    to it are short (2-4 digits, as in "010 9999 0000"), so "…-0001234 2099-01-05" stays two numbers."""
    out = []
    for m in _RUN.finditer(text or ""):
        cur = None
        for t in _TOKEN.finditer(m.group(0)):
            a, b = m.start() + t.start(), m.start() + t.end()
            if cur and len(_groups(cur[2])[-1]) <= 4 and len(_groups(t.group(0))[0]) <= 4:
                cur = (cur[0], b, text[cur[0]:b])
            else:
                if cur:
                    out.append(cur)
                cur = (a, b, t.group(0))
        if cur:
            out.append(cur)
    return out


def id_numbers(text: str) -> list[str]:
    """ID-like numbers (phones, order numbers ...) as written; dates, prices and short numbers left out."""
    return [raw for _, _, raw in spans(text) if _is_id(raw)]


class NumberSlots:
    """One per session (dialog), so a number keeps the same slot name in every call."""

    def __init__(self):
        self.values: list[str] = []

    def _slot(self, raw: str) -> str:
        if raw not in self.values:
            self.values.append(raw)
        return f"[번호{self.values.index(raw) + 1}]"

    def encode(self, text: str) -> str:
        if not text:
            return text
        out, pos = [], 0
        for a, b, raw in spans(text):
            if _is_id(raw):
                out += [text[pos:a], self._slot(raw)]
                pos = b
        return "".join(out) + text[pos:]

    def decode(self, text: str) -> str:
        if not text or "[번호" not in text:
            return text

        def real(m):
            i = int(m.group(1))
            return self.values[i - 1] if 0 < i <= len(self.values) else m.group(0)
        return _SLOT.sub(real, text)

    def table(self) -> str:
        if not self.values:
            return ""
        return TABLE_HEAD + "\n" + "\n".join(f"[번호{i}] = {v}" for i, v in enumerate(self.values, 1))

    def outgoing(self, messages: list[dict]) -> list[dict]:
        """A copy of `messages` for the provider: user contents with slot names, the table at the end
        of the first system message. The input list and its dicts are not changed."""
        out = [{**m, "content": self.encode(m["content"])}
               if m.get("role") == "user" and isinstance(m.get("content"), str) else m for m in messages]
        tbl = self.table()
        if tbl and out and out[0].get("role") == "system":
            out[0] = {**out[0], "content": str(out[0].get("content") or "") + "\n\n" + tbl}
        return out
