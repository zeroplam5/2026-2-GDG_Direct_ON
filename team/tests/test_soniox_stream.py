"""Opt-in AGENT_EOU_MODE=soniox_stream against a LOCAL FAKE Soniox server (no key, no network).

Shows that the session wiring works (frames forwarded, "<end>" -> commit with the streamed text,
resume after "<end>", fallbacks). Says nothing about real Soniox endpoint quality or latency:
compare with team/scripts/sweep_endpoint.py and real audio once the official key is available.
"""
import asyncio
import json

import websockets

from team.agent.config import Config

from .helpers import silence, speechlike
from .test_devui import TEXTS, _debug_client, _devui, cfg, mock  # noqa: F401 (fixtures)

BPS = 32000  # bytes per second at 16 kHz s16le


def fake_soniox(script, seen):
    """script: list of (bytes_received_threshold, message dict) sent once the threshold is reached."""
    async def handler(ws):
        seen["config"] = json.loads(await ws.recv())
        got, i = 0, 0
        async for m in ws:
            if isinstance(m, bytes):
                got += len(m)
                seen["audio"] = got
                while i < len(script) and got >= script[i][0]:
                    await ws.send(json.dumps(script[i][1], ensure_ascii=False))
                    i += 1
            elif m == "":
                seen["eof"] = True
                try:
                    await ws.send(json.dumps({"tokens": [], "finished": True}))
                except websockets.ConnectionClosed:
                    pass
                return
    return handler


def run(cfg, script, pcm, url=None):
    seen = {}

    async def go():
        async with websockets.serve(fake_soniox(script, seen), "127.0.0.1", 0) as fake:
            cfg.soniox_url = url or f"ws://127.0.0.1:{fake.sockets[0].getsockname()[1]}"
            app, srv, port = await _devui(cfg)
            try:
                return await _debug_client(f"ws://127.0.0.1:{port}/ws", "SX-1", cfg._murl, pcm, wait_after_s=1.0)
            finally:
                srv.close()
                await app.http.aclose()

    events, audio = asyncio.run(go())
    by = {}
    for e in events:
        by.setdefault(e["type"], []).append(e)
    return by, seen, audio


def toks(text, end=True):
    t = [{"text": text, "is_final": True}]
    return {"tokens": t + ([{"text": "<end>", "is_final": True}] if end else [])}


def setup(cfg, mock, monkeypatch, **kw):
    monkeypatch.setenv("SONIOX_API_KEY", "test-key")
    cfg.eou_mode = "soniox_stream"
    cfg._murl = mock[1]
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def test_end_token_commits_with_streamed_text(cfg, mock, monkeypatch):
    setup(cfg, mock, monkeypatch)
    streamed = "주문번호 DEMO2002 배송 어디쯤이에요?"
    by, seen, audio = run(cfg, [(int(1.6 * BPS), toks(streamed))], speechlike(1.5) + silence(0.1))
    c = seen["config"]
    assert c["enable_endpoint_detection"] is True and c["sample_rate"] == 16000 and c["api_key"] == "test-key"
    assert c["max_endpoint_delay_ms"] == 1500
    assert by["debug.eou_mode"][0]["mode"] == "soniox_stream"
    assert by["debug.eou"][0]["reason"] == "soniox_end"
    assert by["debug.stt.final"][0]["text"] == streamed           # not the batch fake text
    assert TEXTS["ORDER_OK"] != streamed
    row = by["debug.metrics"][0]["row"]
    assert row["eou_mode"] == "soniox_stream" and row["stt_source"] == "soniox_stream"
    assert by["debug.eou"][0]["silence_ms"] >= 300  # local min-silence rule (frame-counted, not wall clock)
    assert [c["arguments"] for c in by["debug.mock_calls"][0]["calls"]] == [{"order_id": "DEMO2002"}]
    assert "turn.end" in by and audio


def test_resume_after_end_keeps_first_part(cfg, mock, monkeypatch):
    """KO7-like: "<end>" then the customer goes on. The pending end is dropped on the new token and
    the final text holds both parts."""
    setup(cfg, mock, monkeypatch)
    pcm = speechlike(1.0) + silence(0.8) + speechlike(1.0, seed=1) + silence(0.1)
    script = [(int(1.05 * BPS), toks("배송 언제 와요?")),
              (int(1.1 * BPS), {"tokens": [{"text": " 아", "is_final": False}]}),
              (int(2.9 * BPS), toks(" 아 그리고 주문번호는 DEMO1001이요"))]
    by, _, _ = run(cfg, script, pcm)
    sx = [e["event"] for e in by["debug.soniox"]]
    assert sx[:2] == ["end", "resume"]
    assert len(by["debug.eou"]) == 1 and by["debug.eou"][0]["reason"] == "soniox_end"
    assert by["debug.stt.final"][0]["text"] == "배송 언제 와요? 아 그리고 주문번호는 DEMO1001이요"


def test_no_end_token_falls_back_to_local_silence_and_batch_stt(cfg, mock, monkeypatch):
    setup(cfg, mock, monkeypatch, soniox_fallback_silence_ms=800)
    by, _, _ = run(cfg, [], speechlike(1.5) + silence(0.1))
    assert by["debug.eou"][0]["reason"] == "fallback_silence"
    assert by["debug.stt.final"][0]["text"] == TEXTS["ORDER_OK"]  # batch (fake) STT


def test_stream_failure_falls_back_to_vad_mode(cfg, mock, monkeypatch):
    setup(cfg, mock, monkeypatch)
    by, _, _ = run(cfg, [], speechlike(1.5) + silence(0.1), url="ws://127.0.0.1:9")  # nothing listens
    assert by["debug.eou"][0]["reason"] in ("complete_text", "max_silence")
    assert by["debug.stt.final"][0]["text"] == TEXTS["ORDER_OK"]
    assert by["debug.metrics"][0]["row"]["stt_source"] == "batch"


def test_default_mode_is_vad_and_opens_no_stream():
    assert Config().eou_mode == "vad"
