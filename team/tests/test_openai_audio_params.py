"""Offline tests for the OpenAI-specific pieces: streaming resampler, per-model LLM
parameters, STT hedging. No network."""
import asyncio

import numpy as np

from team.agent.audio import StreamResampler, trim_leading_silence
from team.agent.config import Config
from team.agent.providers.llm import model_params


def _tone(rate, sec=1.0, hz=1000):
    t = np.arange(int(rate * sec)) / rate
    return (np.sin(2 * np.pi * hz * t) * 10000).astype(np.int16)


def test_stream_resampler_24k_to_16k_matches_length_and_frequency():
    x = _tone(24000).tobytes()
    rs = StreamResampler(24000, 16000)
    out = b"".join(rs.process(x[i:i + 4801]) for i in range(0, len(x), 4801)) + rs.flush()  # odd chunk sizes
    y = np.frombuffer(out, dtype=np.int16).astype(np.float64)
    assert abs(len(y) - 16000) <= 20
    spec = np.abs(np.fft.rfft(y[2000:14000]))
    peak_hz = np.argmax(spec) * 16000 / len(y[2000:14000])
    assert abs(peak_hz - 1000) < 5
    assert 8000 < np.abs(y[2000:14000]).max() < 11000    # amplitude preserved, no clipping


def test_stream_resampler_no_boundary_clicks():
    x = _tone(24000, hz=300).tobytes()
    rs = StreamResampler(24000, 16000)
    y = np.frombuffer(b"".join(rs.process(x[i:i + 4800]) for i in range(0, len(x), 4800)), dtype=np.int16).astype(float)
    d = np.abs(np.diff(y[200:]))
    assert d.max() < 2 * np.pi * 300 / 16000 * 10000 * 1.2   # max slope of the sine


def test_trim_leading_silence_streaming():
    assert trim_leading_silence(b"\0" * 6400) == b""
    s = b"\0" * 3200 + _tone(16000, 0.1).tobytes()
    assert 3200 - 640 <= len(s) - len(trim_leading_silence(s)) <= 3200


def test_model_params_per_family():
    p = model_params(Config(llm_model="gpt-5"))
    assert p == {"max_completion_tokens": 1024, "reasoning_effort": "minimal"}
    p = model_params(Config(llm_model="gpt-4.1"))
    assert p == {"temperature": 0.2, "max_tokens": 300}
    p = model_params(Config(llm_model="gpt-4o-mini"))
    assert "reasoning_effort" not in p and p["max_tokens"] == 300


def test_stt_hedge_returns_fast_second_request(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "x")
    from team.agent.providers.stt import OpenAISTT
    stt = OpenAISTT(Config(audio_api_key_env="TEST_KEY", stt_hedge_ms=100))
    calls = {"n": 0}

    async def fake_once(wav):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(5)       # pathological tail latency
            return "slow"
        await asyncio.sleep(0.05)
        return "fast"

    stt._once = fake_once
    r = asyncio.run(stt.transcribe(b"\0" * 3200))
    assert r.text == "fast" and r.detail["hedged"] and r.ms < 1000
