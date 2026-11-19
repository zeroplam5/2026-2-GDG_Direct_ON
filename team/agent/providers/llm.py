"""LLM providers with function calling.

stream(messages, tools, tool_choice) yields events:
    ("text", delta)            assistant text, streamed
    ("tool_calls", [ToolCall]) emitted once, at the end of a round that requested tools

OpenAICompatLLM works with any OpenAI-compatible chat-completions endpoint (Upstage Solar,
OpenAI, vLLM, Ollama, llama.cpp server ...) chosen by AGENT_LLM_BASE_URL / AGENT_LLM_MODEL.
The API key is read from the env var named by AGENT_LLM_API_KEY_ENV and never logged.

ScriptedLLM is a TEST DOUBLE (regex rules). It exists only to exercise the pipeline
(tool calling, grounding on tool results, turn.end) without an API key. Its output says
nothing about real AI quality.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as produced by the model


class LLMConfigError(RuntimeError):
    pass


def is_reasoning_model(model: str) -> bool:
    m = model.lower()
    return m.startswith(("gpt-5", "o1", "o3", "o4")) and "chat" not in m


def model_params(cfg) -> dict:
    """Sampling / length parameters per model family.
    Measured 2026-11-18: gpt-5 rejects max_tokens (400) and accepts max_completion_tokens +
    reasoning_effort=minimal; gpt-4.1 / gpt-4o-mini accept temperature + max_tokens."""
    reasoning = is_reasoning_model(cfg.llm_model)
    p = {}
    if not reasoning and cfg.llm_temperature >= 0:
        p["temperature"] = cfg.llm_temperature
    tp = cfg.llm_token_param
    if tp == "auto":
        tp = "max_completion_tokens" if reasoning else "max_tokens"
    if tp in ("max_tokens", "max_completion_tokens"):
        p[tp] = max(cfg.llm_max_tokens, 1024) if reasoning else cfg.llm_max_tokens
    eff = cfg.llm_reasoning_effort
    if eff == "auto":
        # gpt-5.1+ (e.g. gpt-5.4-mini) list none|low|medium|high|xhigh and no "minimal";
        # gpt-5 / o-series accept "minimal" (measured 2026-11-18 for gpt-5).
        eff = ("none" if re.match(r"gpt-5\.\d", cfg.llm_model.lower()) else "minimal") if reasoning else ""
    if eff:
        p["reasoning_effort"] = eff
    return p


class OpenAICompatLLM:
    name = "openai_compat"

    def __init__(self, cfg):
        from openai import AsyncOpenAI
        key = os.environ.get(cfg.llm_api_key_env or "", "")
        if not key and "localhost" not in cfg.llm_base_url and "127.0.0.1" not in cfg.llm_base_url:
            raise LLMConfigError(f"env var {cfg.llm_api_key_env!r} is not set (LLM provider openai_compat)")
        self.cfg = cfg
        self.client = AsyncOpenAI(api_key=key or "local", base_url=cfg.llm_base_url,
                                  timeout=cfg.llm_timeout_s, max_retries=0)
        self.params = model_params(cfg)

    async def stream(self, messages, tools=None, tool_choice="auto"):
        kw = dict(model=self.cfg.llm_model, messages=messages, stream=True, **self.params)
        if tools:
            kw["tools"] = tools
            kw["tool_choice"] = tool_choice
        resp = await self.client.chat.completions.create(**kw)
        calls: dict[int, dict] = {}
        async for chunk in resp:
            if not chunk.choices:
                continue
            d = chunk.choices[0].delta
            if getattr(d, "content", None):
                yield ("text", d.content)
            for tc in (getattr(d, "tool_calls", None) or []):
                slot = calls.setdefault(tc.index or 0, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    slot["arguments"] += tc.function.arguments
        if calls:
            yield ("tool_calls", [ToolCall(c["id"] or f"call_{i}", c["name"], c["arguments"] or "{}")
                                  for i, c in sorted(calls.items())])


_KO_DIGITS = {"공": "0", "영": "0", "일": "1", "이": "2", "삼": "3", "사": "4", "오": "5",
              "육": "6", "륙": "6", "칠": "7", "팔": "8", "구": "9"}


class ScriptedLLM:
    """TEST DOUBLE - not an AI. Recognises an order number and calls get_order_info,
    then reads back the tool result verbatim. Everything else gets a fixed reply."""
    name = "scripted"

    def __init__(self, cfg):
        self.cfg = cfg

    @staticmethod
    def _order_id(text: str):
        """Last order id mentioned (so "1001, 아니 2002" -> 2002). Arabic digits, or a run of
        >=4 Korean digit words; Korean particles such as '이' after digits are not digits."""
        t = text.upper()
        ko = "공영일이삼사오육륙칠팔구"
        found = list(re.finditer(rf"(?:DEMO|데모)[\s-]*(\d[\d\s-]*\d|[{ko}](?:\s*[{ko}]){{3,}})", t))
        if found:
            g = found[-1].group(1)
            digits = "".join(c if c.isdigit() else _KO_DIGITS[c] for c in g if c.isdigit() or c in _KO_DIGITS)
            return "DEMO" + digits
        m = list(re.finditer(r"주문\s*번호[^0-9A-Z]*([A-Z]*\d[\dA-Z-]{3,})", t))
        return m[-1].group(1).replace("-", "") if m else None

    async def stream(self, messages, tools=None, tool_choice="auto"):
        await asyncio.sleep(self.cfg.scripted_delay_ms / 1000)
        names = {t["function"]["name"] for t in (tools or [])}
        last = messages[-1]
        if last["role"] == "tool":
            for part in _render_tool_result(last["content"]):
                yield ("text", part)
            return
        text = last.get("content") or ""
        if re.search(r"상담원|사람(이랑|과|하고)|부작용|두드러기|호흡|응급", text):
            yield ("text", "상담원 확인이 필요한 내용으로 보입니다. 다만 저는 지금 상담원 연결을 직접 완료해 드릴 수 없습니다. ")
            return
        oid = self._order_id(text)
        if oid and "get_order_info" in names and tool_choice != "none":
            yield ("tool_calls", [ToolCall("call_0", "get_order_info", json.dumps({"order_id": oid}))])
            return
        if oid:
            yield ("text", "지금은 주문 조회 기능을 사용할 수 없어 확인해 드리지 못했습니다. ")
            return
        yield ("text", "주문 관련 문의라면 주문번호를 말씀해 주시겠어요? ")


def _render_tool_result(content: str):
    try:
        r = json.loads(content)
    except ValueError:
        r = {"status": "error"}
    st = r.get("status")
    if st == "ok":
        d = r.get("data") or {}
        if "shipping_status" in d:
            yield f"주문번호 {d.get('order_id', '')} 건은 조회 결과 {d['shipping_status']} 상태로 확인됩니다. "
        elif r.get("message"):
            yield f"조회 결과 '{r['message']}'로 확인됩니다. "
        else:
            yield "조회는 되었지만 안내할 수 있는 항목이 없습니다. "
    elif st == "not_found":
        yield "말씀하신 정보로는 확인되는 내역이 없습니다. 번호를 한 번 더 말씀해 주시겠어요? "
    else:
        yield "지금 조회가 원활하지 않아 결과를 확인하지 못했습니다. "
