"""LLM <-> function-calling loop for one session.

run_turn() is an async generator of speakable sentences. Text is streamed sentence by
sentence so TTS can start before the LLM finishes. When the model asks for functions they
are executed through the session's ToolsClient and the results are fed back; the last
allowed round forces tool_choice="none" so the turn always ends with words.
"""
from __future__ import annotations

import asyncio
import json
import time

from . import policy
from .textutil import SentenceSplitter


class Ack(str):
    """A short phrase said while functions run ("확인해 보겠습니다."). The session plays it from
    the pre-synthesized cache and does not count it as the turn's answer, so timeout/error
    fallbacks are still spoken after it."""


def ack_phrases(phrases: dict) -> list[str]:
    v = phrases.get("tool_ack") or []
    return [v] if isinstance(v, str) else [p for p in v if isinstance(p, str) and p.strip()]


class Dialog:
    def __init__(self, llm, tools, system_prompt: str, cfg, phrases: dict):
        self.llm = llm
        self.tools = tools
        self.cfg = cfg
        self.phrases = phrases
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]
        self._acks = ack_phrases(phrases)
        self._ack_i = 0  # rotate so the same phrase is not repeated turn after turn
        self.debug = None  # optional callable(kind, **data); set only by the devui test server

    def _dbg(self, kind, **data):
        if self.debug:
            self.debug(kind, **data)

    async def run_turn(self, user_text: str, m: dict):
        flags = policy.handoff_flags(user_text)
        m["handoff_flags"] = flags
        self.messages.append({"role": "user", "content": policy.user_message(user_text, flags)})
        schemas = self.tools.openai_schemas()
        m.setdefault("llm_rounds", 0)
        m.setdefault("tool_calls", [])
        spoke = False
        for rnd in range(self.cfg.max_tool_rounds + 1):
            last_round = rnd == self.cfg.max_tool_rounds
            splitter = SentenceSplitter(early_min_chars=0 if spoke else self.cfg.first_chunk_min_chars)
            text, calls = "", None
            t_req = time.perf_counter()
            m["llm_rounds"] += 1
            self._dbg("llm.round_start", round=rnd)
            async for kind, val in self.llm.stream(self.messages, tools=schemas or None,
                                                   tool_choice="none" if last_round else "auto"):
                if kind == "text":
                    if "t_llm_first_text" not in m:
                        m["t_llm_first_text"] = time.perf_counter()
                        m["llm_first_text_round_ms"] = round((m["t_llm_first_text"] - t_req) * 1000, 1)
                        self._dbg("llm.first_text", round=rnd)
                    text += val
                    for s in splitter.push(val):
                        spoke = True
                        yield s
                elif kind == "tool_calls" and not last_round:
                    calls = val
            for s in splitter.flush():
                spoke = True
                yield s
            self._dbg("llm.round_end", round=rnd, text=text,
                      tool_calls=[{"name": c.name, "arguments": c.arguments} for c in calls or []])
            if not calls:
                if not text.strip():  # model returned nothing speakable
                    text = self.phrases["tool_round_limit"]
                    yield text
                self.messages.append({"role": "assistant", "content": text})
                return
            self.messages.append({"role": "assistant", "content": text or None, "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in calls]})
            if not spoke and self.cfg.tool_ack and self._acks:
                ack = self._acks[self._ack_i % len(self._acks)]
                self._ack_i += 1
                m["tool_ack"] = ack
                spoke = True
                self._dbg("tool_ack", text=ack)
                yield Ack(ack)  # the session plays it while the functions below run
            t_tools = time.perf_counter()
            results = await asyncio.gather(*(self.tools.call(c.name, c.arguments) for c in calls))
            m["tool_calls"].append({"round": rnd, "ms": round((time.perf_counter() - t_tools) * 1000, 1),
                                    "calls": [{"name": c.name, "status": r.get("status")} for c, r in zip(calls, results)]})
            for c, r in zip(calls, results):
                self.messages.append({"role": "tool", "tool_call_id": c.id,
                                      "content": json.dumps(r, ensure_ascii=False)})
        yield self.phrases["tool_round_limit"]
