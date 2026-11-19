"""LLM preflight: is the key present, which models are visible, does one streamed
function call work? Never prints the key. Sends only a fixed synthetic test sentence.

    .venv/Scripts/python -m team.scripts.check_llm --list
    .venv/Scripts/python -m team.scripts.check_llm --model gpt-4.1-mini
Defaults target OpenAI (base https://api.openai.com/v1, key env OPENAI_API_KEY).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

from team.agent.envfile import load_env_file


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://api.openai.com/v1")
    ap.add_argument("--key-env", default="OPENAI_API_KEY")
    ap.add_argument("--model")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--audio", action="store_true", help="TTS -> STT round trip with the configured audio models")
    ap.add_argument("--env-file", default=".env")
    a = ap.parse_args()
    loaded = load_env_file(a.env_file)
    print(f"names loaded from {a.env_file!r}: {sorted(loaded) or '-'}")
    if a.audio:  # uses the configured providers (AGENT_TTS_PROVIDER / AGENT_STT_PROVIDER), e.g. gemini -> soniox
        from team.agent.config import Config
        from team.agent.providers import make_stt, make_tts
        cfg = Config.from_env()
        tts, stt = make_tts(cfg), make_stt(cfg)
        text = "주문번호 DEMO 일공공일 건은 현재 배송중입니다."
        t = time.perf_counter(); first = None; pcm = b""
        async for ch in tts.stream(text):
            first = first or time.perf_counter() - t
            pcm += ch
        print(f"TTS {tts.name}: first_chunk={first:.2f}s total={time.perf_counter() - t:.2f}s audio={len(pcm) / 32000:.2f}s@16k")
        r = await stt.transcribe(pcm)
        print(f"STT {stt.name}: {r.ms / 1000:.2f}s detail={r.detail} -> {r.text}")
        if not a.model:
            return
    key = os.environ.get(a.key_env)
    print(f"{a.key_env}: {'present' if key else 'MISSING'}")
    if not key:
        return
    from openai import AsyncOpenAI
    c = AsyncOpenAI(api_key=key, base_url=a.base_url, timeout=20, max_retries=0)
    if a.list or not a.model:
        try:
            ids = sorted(m.id for m in (await c.models.list()).data)
            print("models:", ", ".join(i for i in ids if any(s in i for s in ("gpt", "o3", "o4", "solar"))) or ids[:40])
        except Exception as e:
            print("models.list failed:", type(e).__name__, str(e)[:200])
        if not a.model:
            return
    tools = [{"type": "function", "function": {"name": "get_order_info", "description": "Get order status by ID",
              "parameters": {"type": "object", "required": ["order_id"], "properties": {"order_id": {"type": "string"}}}}}]
    t = time.perf_counter()
    first = None
    calls = {}
    try:
        from team.agent.config import Config
        from team.agent.providers.llm import model_params
        stream = await c.chat.completions.create(
            model=a.model, stream=True, tools=tools, **model_params(Config(llm_model=a.model)),
            messages=[{"role": "system", "content": "쇼핑몰 상담원. 주문 조회는 함수로만."},
                      {"role": "user", "content": "주문번호 DEMO1001 배송 어디쯤이에요?"}])
        async for ch in stream:
            if first is None:
                first = time.perf_counter() - t
            if ch.choices:
                for tc in ch.choices[0].delta.tool_calls or []:
                    s = calls.setdefault(tc.index or 0, {"name": "", "arguments": ""})
                    s["name"] += (tc.function.name or "") if tc.function else ""
                    s["arguments"] += (tc.function.arguments or "") if tc.function else ""
        print(f"OK model={a.model} first_chunk={first:.2f}s total={time.perf_counter() - t:.2f}s tool_calls={json.dumps(list(calls.values()), ensure_ascii=False)}")
    except Exception as e:
        print("chat failed:", type(e).__name__, str(e)[:300])


if __name__ == "__main__":
    asyncio.run(main())
