"""Local voice test UI for team members (development tool, NOT the competition server).

Run from the repository root (the mock function server should be running too):
    .venv/Scripts/python -m team.devui.server            # http://127.0.0.1:9100
    .venv/Scripts/python -m team.devui.server --llm scripted --tts tone   # no API key needed

What it serves (one port):
    GET  /            test page (static files in team/devui/static, whitelist only)
    WS   /ws          the SAME Session pipeline as team.agent.server, plus `debug.*` JSON events
                      (VAD/EOU, STT text, LLM rounds, tool arguments/results, timings)

The competition server (team.agent.server, port 9000) is untouched: it never sets
Session.debug, so it sends only PCM, agent.transcript and turn.end. The page can also connect
to that server directly ("호환 모드") to see exactly what the official evaluator would receive.

Binds to 127.0.0.1 by default: browsers only allow microphone access on localhost or HTTPS,
and debug events contain transcripts and function results that should not be exposed on a LAN.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from collections import deque
from pathlib import Path

import httpx
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

from team.agent.config import TEAM_ROOT, Config
from team.agent.envfile import load_env_file
from team.agent.server import App
from team.agent.session import Session

log = logging.getLogger("devui")

STATIC = Path(__file__).resolve().parent / "static"
WS_PATH = "/ws"
# whitelist: URL path -> (file, content type). Nothing else on disk is reachable.
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/pcm.js": ("pcm.js", "text/javascript; charset=utf-8"),
    "/capture-worklet.js": ("capture-worklet.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
DEBUG_QUEUE_MAX = 2000

# config values shown in the page (no keys: public_dict only has env-var *names*, and we
# pick a subset anyway)
HELLO_FIELDS = ("stt_provider", "stt_model", "llm_provider", "llm_model", "tts_provider", "tts_model", "tts_voice",
                "vad", "energy_min_rms", "min_speech_ms", "stt_trigger_silence_ms", "semantic_eou",
                "complete_silence_ms", "max_silence_ms", "max_utterance_ms", "turn_budget_s", "tool_timeout_s",
                "output_pacing", "send_transcript_event", "tool_gate", "output_guard", "tool_ack",
                "first_chunk_min_chars", "pack_dir", "eou_mode", "soniox_max_endpoint_delay_ms",
                "soniox_min_silence_ms", "soniox_fallback_silence_ms")


def _response(status: int, reason: str, body: bytes, ctype: str) -> Response:
    return Response(status, reason, Headers([("Content-Type", ctype), ("Content-Length", str(len(body))),
                                             ("Cache-Control", "no-store"),
                                             ("X-Content-Type-Options", "nosniff")]), body)


def process_request(connection, request):
    path = request.path.split("?", 1)[0]
    if path == WS_PATH:
        return None  # continue with the WebSocket handshake
    entry = FILES.get(path)
    if entry is None:
        return _response(404, "Not Found", b"not found", "text/plain; charset=utf-8")
    return _response(200, "OK", (STATIC / entry[0]).read_bytes(), entry[1])


class DebugSocket:
    """Wraps the connection given to Session so debug events keep their order relative to the
    session's own messages: before every send of the session (PCM, agent.transcript, turn.end)
    the debug events raised so far are flushed first. Events raised while the session is not
    sending go out through pump(). Everything else (recv, iteration, close) is delegated."""

    def __init__(self, ws):
        self.ws = ws
        self.pending: deque = deque()
        self.dropped = 0
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self.ws, name)

    def __aiter__(self):
        return self.ws.__aiter__()

    def emit(self, evt: dict):
        if len(self.pending) >= DEBUG_QUEUE_MAX:
            self.dropped += 1
            return
        if self.dropped:
            evt = {**evt, "dropped_before": self.dropped}
            self.dropped = 0
        self.pending.append(evt)
        self.wake.set()

    async def _flush(self):
        while self.pending:
            await self.ws.send(json.dumps(self.pending.popleft(), ensure_ascii=False, default=str))

    async def send(self, data):
        async with self.lock:
            await self._flush()
            await self.ws.send(data)

    async def pump(self):
        try:
            while True:
                await self.wake.wait()
                self.wake.clear()
                async with self.lock:
                    await self._flush()
        except Exception:  # connection closed
            return


def make_handler(app: App):
    async def handler(ws):
        dws = DebugSocket(ws)
        sess = Session(dws, app)
        side_tasks: set[asyncio.Task] = set()

        def sink(evt: dict):
            dws.emit(evt)
            if evt.get("type") == "debug.metrics":
                # cross-check: what the function server itself recorded for this session
                t = asyncio.create_task(fetch_calls(sess, sink))
                side_tasks.add(t)
                t.add_done_callback(side_tasks.discard)

        sess.debug = sink
        pump_task = asyncio.create_task(dws.pump())
        cfg = app.cfg
        sink({"type": "debug.hello", "server": "team.devui", "ws_path": WS_PATH,
              "config": {k: getattr(cfg, k) for k in HELLO_FIELDS},
              "providers": {"stt": app.stt.name, "llm": app.llm.name, "tts": app.tts.name}})
        try:
            await sess.run()
        finally:
            for t in list(side_tasks):
                t.cancel()
            pump_task.cancel()

    return handler


async def fetch_calls(sess: Session, sink):
    base = getattr(getattr(sess, "tools", None), "base", "")
    if not base:
        sink({"type": "debug.mock_calls", "error": "no tools_url"})
        return
    try:
        r = await sess.app.http.get(f"{base}/calls", params={"session_id": sess.sid}, timeout=3.0)
        r.raise_for_status()
        sink({"type": "debug.mock_calls", "calls": r.json()})
    except Exception as e:  # the official relay may not provide /calls at all
        sink({"type": "debug.mock_calls", "error": f"{type(e).__name__}: {str(e)[:120]}"})


async def amain(cfg: Config):
    app = App(cfg)
    await app.warm()
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=20)) as http:
        app.http = http
        async with websockets.serve(make_handler(app), cfg.host, cfg.port, process_request=process_request,
                                    max_size=None, ping_interval=None):
            log.info("voice test UI: http://%s:%s/  (debug WebSocket %s)",
                     "127.0.0.1" if cfg.host in ("0.0.0.0", "127.0.0.1") else cfg.host, cfg.port, WS_PATH)
            await asyncio.Future()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--stt", dest="stt_provider", help="openai (default) | soniox | faster_whisper | fake")
    ap.add_argument("--stt-model", dest="stt_model")
    ap.add_argument("--llm", dest="llm_provider", help="openai_compat (default) | scripted (test double)")
    ap.add_argument("--llm-model", dest="llm_model")
    ap.add_argument("--tts", dest="tts_provider", help="openai (default) | gemini | sapi | tone (test double)")
    ap.add_argument("--max-silence-ms", dest="max_silence_ms", type=int)
    ap.add_argument("--complete-silence-ms", dest="complete_silence_ms", type=int)
    ap.add_argument("--metrics", dest="metrics_path", default=str(TEAM_ROOT / "runs" / "devui_metrics.jsonl"),
                    help="timing log (no transcripts unless AGENT_LOG_TEXT=1)")
    ap.add_argument("--env-file", default=".env", help="KEY=VALUE file loaded at start (existing env wins); '' to disable")
    a = vars(ap.parse_args())
    loaded = load_env_file(a.pop("env_file"))
    cfg = Config.from_env(**a)
    logging.basicConfig(level=os.environ.get("AGENT_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if loaded:
        log.info("loaded env names from file: %s", ", ".join(sorted(loaded)))
    log.info("config %s", json.dumps(cfg.public_dict(), ensure_ascii=False))
    if cfg.stt_provider == "fake":
        log.warning("STT is the fake test double: it does NOT listen to the microphone and returns %s. "
                    "For real recognition use --stt faster_whisper (local, no key) or --stt openai (key).",
                    repr(cfg.stt_fake_text) if cfg.stt_fake_text else "an empty text (AGENT_STT_FAKE_TEXT is not set)")
    asyncio.run(amain(cfg))


if __name__ == "__main__":
    main()
