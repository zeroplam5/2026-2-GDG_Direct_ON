"""DIRECT:ON practice baseline voice agent - WebSocket server.

Run from the repository root:
    .venv/Scripts/python -m team.agent.server --port 9000
Providers / thresholds: see team/agent/config.py (env vars AGENT_*) or --help.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

import httpx
import websockets

from . import policy
from .config import Config, repo_path
from .envfile import load_env_file
from .providers import make_llm, make_stt, make_tts
from .session import Session
from .textutil import normalize_for_tts

log = logging.getLogger("agent")


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # official brand material (kit data/brand) goes right after our instructions
        self.brand = policy.load_brand(repo_path(cfg.brand_dir))
        if self.brand:
            log.info("brand material from %s: scenarios=%s information=%d style=%s", cfg.brand_dir,
                     "scenarios" in self.brand, len(self.brand.get("information") or []), "style" in self.brand)
        else:
            log.warning("no brand material at %s (set AGENT_BRAND_DIR to the kit's data/brand)", cfg.brand_dir)
        self.prompt_template = policy.compose_prompt(policy.load_prompt(cfg.prompt_path), self.brand)
        self.phrases = policy.load_phrases(cfg.phrases_path)
        self.rules = policy.load_rules(cfg.rules_path)  # the one rules file (gate, hand-off, prompt section)
        t = time.perf_counter()
        self.stt = make_stt(cfg)
        self.llm = make_llm(cfg)
        self.tts = make_tts(cfg)
        log.info("providers ready in %.1fs: stt=%s llm=%s(%s) tts=%s", time.perf_counter() - t,
                 self.stt.name, self.llm.name, cfg.llm_model if self.llm.name == "openai_compat" else "-", self.tts.name)
        self.cached: dict[str, bytes] = {}       # phrase key -> PCM (fallback phrases)
        self.cached_text: dict[str, bytes] = {}  # exact text -> PCM (tool_ack phrases, played without TTS)
        self.http: httpx.AsyncClient | None = None
        self._mlock = threading.Lock()
        Path(cfg.metrics_path).parent.mkdir(parents=True, exist_ok=True)

    async def warm(self):
        for k in ("not_heard", "delay_fallback", "error_fallback"):
            try:  # a slow/failed TTS call must not stop the server; the phrase is synthesized on demand later
                self.cached[k] = await self.tts.synth(self.phrases[k])
            except Exception as e:
                log.warning("pre-synthesis of %r failed (%s); will synthesize on demand", k, type(e).__name__)
        from .dialog import ack_phrases
        for text in (ack_phrases(self.phrases) if self.cfg.tool_ack else []):
            try:
                self.cached_text[text] = await self.tts.synth(normalize_for_tts(text))
            except Exception as e:
                log.warning("pre-synthesis of tool_ack %r failed (%s); will synthesize on demand", text, type(e).__name__)

    def write_metrics(self, row: dict):
        with self._mlock, open(self.cfg.metrics_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


async def amain(cfg: Config):
    app = App(cfg)
    await app.warm()
    limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)
    async with httpx.AsyncClient(limits=limits) as http:
        app.http = http

        async def handler(ws):
            await Session(ws, app).run()

        async with websockets.serve(handler, cfg.host, cfg.port, max_size=None, ping_interval=None):
            log.info("agent listening on ws://%s:%s", cfg.host, cfg.port)
            await asyncio.Future()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int)
    ap.add_argument("--host")
    ap.add_argument("--stt", dest="stt_provider", help="faster_whisper | fake")
    ap.add_argument("--stt-model", dest="stt_model")
    ap.add_argument("--stt-device", dest="stt_device")
    ap.add_argument("--llm", dest="llm_provider", help="openai_compat | scripted (test double)")
    ap.add_argument("--llm-model", dest="llm_model")
    ap.add_argument("--llm-base-url", dest="llm_base_url")
    ap.add_argument("--tts", dest="tts_provider", help="sapi | tone (test double)")
    ap.add_argument("--max-silence-ms", dest="max_silence_ms", type=int)
    ap.add_argument("--complete-silence-ms", dest="complete_silence_ms", type=int)
    ap.add_argument("--metrics", dest="metrics_path")
    ap.add_argument("--log-text", dest="log_text", action="store_true", default=None)
    ap.add_argument("--env-file", default=".env", help="KEY=VALUE file loaded at start (existing env wins); '' to disable")
    a = vars(ap.parse_args())
    loaded = load_env_file(a.pop("env_file"))
    cfg = Config.from_env(**a)
    logging.basicConfig(level=os.environ.get("AGENT_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if loaded:
        log.info("loaded env names from file: %s", ", ".join(sorted(loaded)))
    log.info("config %s", json.dumps(cfg.public_dict(), ensure_ascii=False))
    asyncio.run(amain(cfg))


if __name__ == "__main__":
    main()
