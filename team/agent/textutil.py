"""Korean text heuristics used around the LLM: utterance completeness (for endpointing),
sentence splitting (for streaming TTS) and TTS text normalization.

These are practice heuristics, not official rules. They are deliberately conservative:
when unsure, `looks_complete` returns None and the session waits for max_silence_ms.
"""
from __future__ import annotations

import re

# Endings that usually close a Korean sentence (polite/plain/question forms).
_FINAL_ENDINGS = (
    "요", "다", "까", "죠", "네", "니다", "세요", "에요", "예요", "어요", "아요", "지요",
    "나요", "가요", "래요", "군요", "구요", "거든요", "잖아요", "할게요", "주세요", "줘",
    "해", "돼", "야", "지", "니", "냐", "어", "아",
)
# Endings / words that usually mean "I am not done yet".
_CONTINUATION = (
    "는데", "은데", "인데", "한데", "던데", "고", "서", "면", "며", "랑", "하고", "이랑",
    "그리고", "근데", "그런데", "그래서", "그러니까", "그니까", "아니면", "또", "및",
    "음", "어", "저", "그", "뭐", "그게", "저기", "이게", "번호는", "번호가", "주문번호는",
    "에서", "으로", "로", "의", "를", "을", "가", "이", "는", "은", "도", "에",
)
_WORD_FILLERS = {"음", "어", "저", "그", "뭐", "저기", "그게", "아", "에"}


def looks_complete(text: str) -> bool | None:
    """True: likely a finished turn. False: likely mid-sentence. None: unknown."""
    t = (text or "").strip()
    if not t:
        return None
    if t.endswith(("...", "…", ",", "-")):
        return False
    core = re.sub(r"[\s.!?~。]+$", "", t)
    if not core:
        return None
    last_word = core.split()[-1]
    if last_word in _WORD_FILLERS:
        return False
    if t[-1] == "?":
        return True
    # digits at the end: often an order/phone number being read out -> unknown
    if re.search(r"\d$", core):
        return None
    # connectives such as "~는데", "~고", "그리고" -> not finished.
    # ("어"/"아"/"지" are both final and connective, so check final endings first for
    #  the longer polite forms, then continuation markers.)
    for e in sorted(_FINAL_ENDINGS, key=len, reverse=True):
        if len(e) >= 2 and core.endswith(e):
            return True
    for e in sorted(_CONTINUATION, key=len, reverse=True):
        if core.endswith(e):
            return False
    for e in _FINAL_ENDINGS:
        if core.endswith(e):
            return True
    if re.search(r"[A-Za-z]$", core):  # English (e.g. starter-kit demo audio)
        return True if t[-1] in ".!?" else None
    return None


class SentenceSplitter:
    """Accumulates streamed LLM text and yields sentences as soon as they are complete.

    A sentence ends at . ! ? followed by whitespace; punctuation at the very end of the
    buffer waits for the next delta (could be a decimal point). Very short sentences
    ("네.") are merged with the next one so TTS is not called for a single syllable."""

    _END = re.compile(r"[.!?。]+\s")
    _COMMA = re.compile(r"[,，]\s")

    def __init__(self, min_chars: int = 6, early_min_chars: int = 0):
        """early_min_chars > 0: the FIRST chunk may also end at ", " once it is at least this
        long, so TTS can start before the first sentence is complete (later chunks: sentences)."""
        self.buf = ""
        self.pending = ""
        self.min_chars = min_chars
        self.early_min_chars = early_min_chars
        self.emitted = False

    def push(self, delta: str):
        self.buf += delta
        out = self._push()
        if out:
            self.emitted = True
        return out

    def _push(self):
        out = []
        if self.early_min_chars and not self.emitted and not self.pending:
            end = self._END.search(self.buf)
            for c in self._COMMA.finditer(self.buf):
                if end and end.start() < c.start():
                    break  # a full sentence comes first -> normal path
                if c.start() >= self.early_min_chars:
                    out.append(self.buf[:c.end()].strip())
                    self.buf = self.buf[c.end():]
                    break
        while True:
            m = self._END.search(self.buf)
            if not m:
                break
            sent = self.buf[:m.end()].strip()
            self.buf = self.buf[m.end():]
            self.pending = (self.pending + " " + sent).strip()
            if len(self.pending) >= self.min_chars:
                out.append(self.pending)
                self.pending = ""
        return out

    def flush(self):
        s = (self.pending + " " + self.buf).strip()
        self.pending, self.buf = "", ""
        return [s] if s else []


_DIGIT_KO = "공일이삼사오육칠팔구"


def normalize_for_tts(text: str) -> str:
    """Read long digit strings (order / phone numbers) digit by digit; strip markdown."""
    t = re.sub(r"[*#`_>|]", "", text)

    def digits(m):
        s = m.group(0)
        return " ".join(_DIGIT_KO[int(c)] for c in s)

    t = re.sub(r"\d{5,}", digits, t)
    t = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", t)  # DEMO1001 -> DEMO 1001
    t = re.sub(r"(?<=[A-Za-z] )\d{2,4}\b", digits, t)
    return t
