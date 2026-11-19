"""Local voice test UI server (team.devui) with TEST-DOUBLE providers.

Checks that
* the debug path (/ws) runs the same Session pipeline and adds debug.* events with the STT text,
  tool arguments/results and timings, consistent with what the mock function server recorded;
* the competition path (team.agent.server handler = Session without debug) still sends only
  PCM + agent.transcript + turn.end;
* the static file server only serves its whitelist.
"""
import asyncio
import json
import threading
import time

import httpx
import pytest
import websockets

from team.agent.config import Config
from team.agent.server import App
from team.agent.session import Session
from team.devui.server import make_handler, process_request
from team.mock.mock_api import serve

from .helpers import run_client, silence, speechlike

TEXTS = {"ORDER_OK": "주문번호 DEMO1001 배송 어디쯤이에요?"}
RATE, FRAME = 16000, 640


@pytest.fixture
def mock():
    srv, store = serve(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield store, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def cfg(tmp_path):
    mapping = tmp_path / "stt.json"
    mapping.write_text(json.dumps(TEXTS, ensure_ascii=False), encoding="utf-8")
    return Config(port=0, vad="energy", stt_provider="fake", stt_fake_text=str(mapping),
                  llm_provider="scripted", tts_provider="tone", metrics_path=str(tmp_path / "m.jsonl"),
                  tool_timeout_s=2.0)


async def _app(cfg):
    app = App(cfg)
    await app.warm()
    app.http = httpx.AsyncClient()
    return app


async def _devui(cfg):
    app = await _app(cfg)
    srv = await websockets.serve(make_handler(app), "127.0.0.1", 0, process_request=process_request,
                                 max_size=None, ping_interval=None)
    return app, srv, srv.sockets[0].getsockname()[1]


async def _debug_client(url, sid, tools_url, pcm, speed=4.0, wait_after_s=3.0):
    """Like the page: session.start, frames (speech then silence) until turn.end, then keep the
    socket open until debug.mock_calls arrives."""
    events, audio = [], bytearray()
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "session.start", "session_id": sid, "case_id": "ORDER_OK",
                                  "now": "2026-11-21 11:00", "history": [], "caller": None, "tools_url": tools_url,
                                  "audio": {"encoding": "pcm_s16le", "sample_rate": RATE, "channels": 1}}))
        turn_end = asyncio.Event()
        t0 = time.perf_counter()

        async def sender():
            k = 0
            while not turn_end.is_set():
                c = pcm[k * FRAME:(k + 1) * FRAME]
                await ws.send(c + b"\0" * (FRAME - len(c)))
                k += 1
                w = t0 + k * 0.02 / speed - time.perf_counter()
                if w > 0:
                    await asyncio.sleep(w)

        st = asyncio.create_task(sender())
        deadline = time.perf_counter() + 20
        try:
            while time.perf_counter() < deadline:
                try:
                    m = await asyncio.wait_for(ws.recv(), max(0.05, deadline - time.perf_counter()))
                except asyncio.TimeoutError:
                    break
                if isinstance(m, bytes):
                    audio.extend(m)
                    continue
                e = json.loads(m)
                events.append(e)
                if e["type"] == "turn.end":
                    turn_end.set()
                    deadline = min(deadline, time.perf_counter() + wait_after_s)
                if e["type"] == "debug.mock_calls":
                    break
        finally:
            st.cancel()
    return events, bytes(audio)


def test_debug_path_reports_pipeline_and_matches_function_server(cfg, mock):
    store, murl = mock

    async def go():
        app, srv, port = await _devui(cfg)
        try:
            return await _debug_client(f"ws://127.0.0.1:{port}/ws", "UI-T1", murl,
                                       speechlike(1.5) + silence(0.2))
        finally:
            srv.close()
            await app.http.aclose()

    events, audio = asyncio.run(go())
    types = [e["type"] for e in events]
    for t in ("debug.hello", "debug.session", "debug.tools", "debug.vad", "debug.eou", "debug.stt",
              "debug.stt.final", "debug.llm.round_start", "debug.tool.start", "debug.tool.end",
              "debug.audio.first_sent", "agent.transcript", "turn.end", "debug.metrics", "debug.mock_calls"):
        assert t in types, (t, types)
    by = {}
    for e in events:
        by.setdefault(e["type"], []).append(e)

    # debug events keep their order relative to the session's own messages
    idx = {t: types.index(t) for t in set(types)}
    assert idx["debug.eou"] < idx["debug.stt.final"] < idx["debug.tool.start"] < idx["debug.tool.end"] \
        < idx["agent.transcript"] < idx["turn.end"] < idx["debug.metrics"] < idx["debug.mock_calls"]

    hello = by["debug.hello"][0]
    assert hello["config"]["stt_model"] == cfg.stt_model and "api_key" not in json.dumps(hello).lower()
    assert by["debug.stt.final"][0]["text"] == TEXTS["ORDER_OK"]
    eou = by["debug.eou"][0]
    assert eou["reason"] and eou["t_commit_ms"] >= eou["t_last_speech_ms"]

    start, end = by["debug.tool.start"][0], by["debug.tool.end"][0]
    assert start["name"] == "get_order_info" and json.loads(start["arguments"]) == {"order_id": "DEMO1001"}
    assert end["sent"] is True and end["http_status"] == 200 and end["result"]["status"] != "error"
    # what the page shows == what the function server recorded for this session (X-Session-Id)
    recorded = by["debug.mock_calls"][0]["calls"]
    assert [(c["name"], c["arguments"]) for c in recorded] == [("get_order_info", {"order_id": "DEMO1001"})]
    assert [c["session_id"] for c in store.calls] == ["UI-T1"]

    # debug times are on one server clock: commit < first audio sent < turn.end
    first_audio = by["debug.audio.first_sent"][0]["t_ms"]
    assert eou["t_commit_ms"] <= start["t_ms"] <= end["t_ms"] <= first_audio <= by["debug.turn_end_sent"][0]["t_ms"]
    assert audio and len(audio) % 2 == 0
    row = by["debug.metrics"][0]
    assert row["user_text"] == TEXTS["ORDER_OK"] and "배송중" in row["reply_text"]
    # transcripts are given to the debug client but not written to the metrics file (log_text off)
    on_disk = [json.loads(l) for l in open(cfg.metrics_path, encoding="utf-8")]
    assert on_disk and "user_text" not in on_disk[-1]


def test_competition_path_sends_no_debug_events(cfg, mock):
    _, murl = mock

    async def go():
        app = await _app(cfg)
        srv = await websockets.serve(lambda ws: Session(ws, app).run(), "127.0.0.1", 0, max_size=None)
        try:
            url = f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
            return await run_client(url, "SID-PLAIN", speechlike(1.5) + silence(0.2), murl,
                                    case_id="ORDER_OK", speed=4)
        finally:
            srv.close()
            await app.http.aclose()

    r = asyncio.run(go())
    assert r["turn_end"] and r["audio_bytes"] > 0
    assert {e["type"] for e in r["events"]} <= {"agent.transcript", "turn.end"}
    assert r["events"][-1]["type"] == "turn.end"


def test_static_whitelist_and_ws_path(cfg):
    async def go():
        app, srv, port = await _devui(cfg)
        base = f"http://127.0.0.1:{port}"
        try:
            async with httpx.AsyncClient() as c:
                idx = await c.get(base + "/")
                js = await c.get(base + "/pcm.js")
                bad = [await c.get(base + p) for p in ("/../agent/config.py", "/.env", "/static/app.js", "/server.py")]
            # WebSocket upgrade on any path other than /ws is refused (no accidental evaluator use)
            with pytest.raises(websockets.InvalidStatus):
                async with websockets.connect(f"ws://127.0.0.1:{port}/"):
                    pass
            return idx, js, bad
        finally:
            srv.close()
            await app.http.aclose()

    idx, js, bad = asyncio.run(go())
    assert idx.status_code == 200 and idx.headers["content-type"].startswith("text/html") and "테스트 시작" in idx.text
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert all(r.status_code == 404 for r in bad)
