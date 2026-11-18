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


class Dialog:
    def __init__(self, llm, tools, system_prompt: str, cfg, phrases: dict):
        self.llm = llm
        self.tools = tools
        self.cfg = cfg
        self.phrases = phrases
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]

    async def run_turn(self, user_text: str, m: dict):
        flags = policy.handoff_flags(user_text)
        m["handoff_flags"] = flags
        self.messages.append({"role": "user", "content": policy.user_message(user_text, flags)})
        schemas = self.tools.openai_schemas()
        m.setdefault("llm_rounds", 0)
        m.setdefault("tool_calls", [])
        for rnd in range(self.cfg.max_tool_rounds + 1):
            last_round = rnd == self.cfg.max_tool_rounds
            splitter = SentenceSplitter()
            text, calls = "", None
            t_req = time.perf_counter()
            m["llm_rounds"] += 1
            async for kind, val in self.llm.stream(self.messages, tools=schemas or None,
                                                   tool_choice="none" if last_round else "auto"):
                if kind == "text":
                    if "t_llm_first_text" not in m:
                        m["t_llm_first_text"] = time.perf_counter()
                        m["llm_first_text_round_ms"] = round((m["t_llm_first_text"] - t_req) * 1000, 1)
                    text += val
                    for s in splitter.push(val):
                        yield s
                elif kind == "tool_calls" and not last_round:
                    calls = val
            for s in splitter.flush():
                yield s
            if not calls:
                if not text.strip():  # model returned nothing speakable
                    text = self.phrases["tool_round_limit"]
                    yield text
                self.messages.append({"role": "assistant", "content": text})
                return
            self.messages.append({"role": "assistant", "content": text or None, "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in calls]})
            t_tools = time.perf_counter()
            results = await asyncio.gather(*(self.tools.call(c.name, c.arguments) for c in calls))
            m["tool_calls"].append({"round": rnd, "ms": round((time.perf_counter() - t_tools) * 1000, 1),
                                    "calls": [{"name": c.name, "status": r.get("status")} for c, r in zip(calls, results)]})
            for c, r in zip(calls, results):
                self.messages.append({"role": "tool", "tool_call_id": c.id,
                                      "content": json.dumps(r, ensure_ascii=False)})
        yield self.phrases["tool_round_limit"]
