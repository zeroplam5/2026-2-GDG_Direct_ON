"""Wire-format tests for the 11/21 official providers (Soniox STT, Gemini TTS) and the
gpt-5.4-mini request parameters, against local fakes that mimic the documented formats.
No API key needed. Says nothing about real latency or quality - run
`python -m team.scripts.check_llm --audio` with real keys for that."""
import asyncio
import base64
import json

import httpx
import numpy as np
import websockets

from team.agent.config import Config
from team.agent.providers.llm import model_params
from team.agent.providers.stt import SonioxSTT
from team.agent.providers.tts import GeminiTTS


def test_soniox_sends_config_audio_and_collects_finals(monkeypatch):
    seen = {"config": None, "audio": 0, "eof": False}

    async def fake(ws):
        seen["config"] = json.loads(await ws.recv())
        async for m in ws:
            if isinstance(m, bytes):
                seen["audio"] += len(m)
                if seen["audio"] >= 6400 and not seen.get("sent_partial"):
                    seen["sent_partial"] = True
                    await ws.send(json.dumps({"tokens": [{"text": "주문", "is_final": True},
                                                         {"text": "번호", "is_final": False}]}))
            elif m == "":
                seen["eof"] = True
                await ws.send(json.dumps({"tokens": [{"text": "번호 DEMO1001", "is_final": True},
                                                     {"text": "<end>", "is_final": True}]}))
                await ws.send(json.dumps({"tokens": [], "finished": True}))
                return

    async def run():
        async with websockets.serve(fake, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            monkeypatch.setenv("SONIOX_API_KEY", "test-key")
            stt = SonioxSTT(Config(soniox_url=f"ws://127.0.0.1:{port}", soniox_model="stt-rt-test"))
            return await stt.transcribe(b"\0" * 32000)  # 1 s

    res = asyncio.run(run())
    c = seen["config"]
    assert c["audio_format"] == "pcm_s16le" and c["sample_rate"] == 16000 and c["num_channels"] == 1
    assert c["model"] == "stt-rt-test" and c["language_hints"] == ["ko"] and c["api_key"] == "test-key"
    assert seen["audio"] == 32000 and seen["eof"]
    assert res.text == "주문번호 DEMO1001"
    assert res.detail == {"unfinalized": False}


def test_soniox_error_message_raises(monkeypatch):
    async def fake(ws):
        await ws.recv()
        await ws.send(json.dumps({"error_code": 400, "error_message": "Invalid model"}))

    async def run():
        async with websockets.serve(fake, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            monkeypatch.setenv("SONIOX_API_KEY", "k")
            await SonioxSTT(Config(soniox_url=f"ws://127.0.0.1:{port}")).transcribe(b"\0" * 640)

    try:
        asyncio.run(run())
    except RuntimeError as e:
        assert "Invalid model" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def _gemini_fake(pcm24: bytes, seen: list):
    b64 = base64.b64encode(pcm24).decode()
    obj = {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000", "data": b64}}]}}]}

    def handler(req: httpx.Request):
        seen.append((req.url.path, req.headers.get("x-goog-api-key"), json.loads(req.content)))
        if req.url.path.endswith(":streamGenerateContent"):
            return httpx.Response(200, text=f"data: {json.dumps(obj)}\n\ndata: {json.dumps(obj)}\n\n")
        return httpx.Response(200, json=obj)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_gemini_tts_resamples_24k_to_16k(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    pcm24 = (np.sin(np.arange(24000) / 4) * 8000).astype(np.int16).tobytes()  # 1 s @ 24 kHz
    seen = []
    tts = GeminiTTS(Config(gemini_tts_voice="Kore"), client=_gemini_fake(pcm24, seen))
    out = asyncio.run(tts.synth("안녕하세요"))
    assert abs(len(out) - 32000) <= 64  # 1 s @ 16 kHz
    path, key, body = seen[0]
    assert path.endswith("/models/gemini-2.5-flash-preview-tts:generateContent") and key == "g-key"
    assert body["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert body["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"
    assert body["contents"][0]["parts"][0]["text"] == "안녕하세요"


def test_gemini_tts_stream_mode(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    pcm24 = (np.sin(np.arange(2400) / 4) * 8000).astype(np.int16).tobytes()  # 0.1 s
    seen = []
    tts = GeminiTTS(Config(gemini_tts_stream=True), client=_gemini_fake(pcm24, seen))

    async def run():
        return [c async for c in tts.stream("네")]

    chunks = asyncio.run(run())
    assert seen[0][0].endswith(":streamGenerateContent")
    assert abs(sum(map(len, chunks)) - 6400) <= 64  # two 0.1 s parts -> 0.2 s @ 16 kHz


def test_reasoning_effort_per_model_family():
    p54 = model_params(Config(llm_model="gpt-5.4-mini"))
    assert p54.get("reasoning_effort") == "none" and "max_completion_tokens" in p54 and "temperature" not in p54
    assert model_params(Config(llm_model="gpt-5")).get("reasoning_effort") == "minimal"
    p41 = model_params(Config(llm_model="gpt-4.1"))
    assert "reasoning_effort" not in p41 and "max_tokens" in p41


import pytest  # noqa: E402


@pytest.mark.parametrize("mode,header,in_config", [
    ("config", None, True),                 # default: unchanged behaviour
    ("header", "Bearer test-key", False),   # contest site example (11/20)
    ("both", "Bearer test-key", True),
])
def test_soniox_auth_modes_on_the_wire(monkeypatch, mode, header, in_config):
    seen = {}

    async def fake(ws):
        seen["auth"] = ws.request.headers.get("Authorization")
        seen["config"] = json.loads(await ws.recv())
        async for m in ws:
            if m == "":
                await ws.send(json.dumps({"tokens": [{"text": "네", "is_final": True}], "finished": True}))
                return

    async def run():
        async with websockets.serve(fake, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            monkeypatch.setenv("SONIOX_API_KEY", "test-key")
            return await SonioxSTT(Config(soniox_url=f"ws://127.0.0.1:{port}", soniox_auth=mode)).transcribe(b"\0" * 640)

    assert asyncio.run(run()).text == "네"
    assert seen["auth"] == header
    assert ("api_key" in seen["config"]) is in_config


def test_soniox_auth_default_is_unchanged():
    assert Config().soniox_auth == "config"
