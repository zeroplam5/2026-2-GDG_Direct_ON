"""Single-provider checks for the 11/21 official audio models (one short request each).

    # STT alone on one practice file (uses AGENT_STT_PROVIDER, e.g. soniox)
    .venv/Scripts/python -m team.scripts.check_audio --stt team/data/practice_ko/audio/K01.wav
    # TTS alone; writes 16 kHz s16le mono PCM as a wav and plays it (Windows: winsound)
    .venv/Scripts/python -m team.scripts.check_audio --tts "주문 내역을 확인해 보겠습니다." --play

Uses the same provider classes and AGENT_* settings as the agent (reads .env). Never prints
key values. Each run costs one API request; the TTS sentence is a fixed synthetic sentence by
default. Output wav files go to team/runs/ (*.wav is git-ignored).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import wave
from pathlib import Path

from team.agent.audio import RATE
from team.agent.config import TEAM_ROOT, Config
from team.agent.envfile import load_env_file
from team.agent.providers import make_stt, make_tts

DEFAULT_SENTENCE = "주문 내역을 확인해 보겠습니다. 주문번호 데모 일공공일 건은 배송 중입니다."


def read_wav_16k_mono(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            raise SystemExit(f"{path}: need 16 kHz mono s16le, got {w.getframerate()} Hz {w.getnchannels()}ch "
                             f"{8 * w.getsampwidth()}-bit")
        return w.readframes(w.getnframes())


async def check_stt(cfg: Config, path: str) -> dict:
    stt = make_stt(cfg)
    pcm = read_wav_16k_mono(path)
    t = time.perf_counter()
    res = await stt.transcribe(pcm, {"case_id": Path(path).stem, "session_id": "check_audio"})
    return {"provider": stt.name, "audio_s": round(len(pcm) / 2 / RATE, 2), "text": res.text,
            "provider_ms": round(res.ms, 1), "wall_ms": round((time.perf_counter() - t) * 1000, 1),
            "detail": res.detail}


async def check_tts(cfg: Config, text: str, out: Path) -> dict:
    tts = make_tts(cfg)
    t = time.perf_counter()
    first_ms, chunks = None, []
    if hasattr(tts, "stream"):
        async for c in tts.stream(text):
            if first_ms is None:
                first_ms = round((time.perf_counter() - t) * 1000, 1)
            chunks.append(c)
    else:
        chunks.append(await tts.synth(text))
        first_ms = round((time.perf_counter() - t) * 1000, 1)
    pcm = b"".join(chunks)
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return {"provider": tts.name, "chars": len(text), "first_audio_ms": first_ms,
            "total_ms": round((time.perf_counter() - t) * 1000, 1), "chunks": len(chunks),
            "bytes": len(pcm), "even_bytes": len(pcm) % 2 == 0, "audio_s": round(len(pcm) / 2 / RATE, 2),
            "wav": str(out)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stt", metavar="WAV", help="16 kHz mono wav to transcribe")
    ap.add_argument("--tts", metavar="TEXT", nargs="?", const=DEFAULT_SENTENCE, help="sentence to synthesize")
    ap.add_argument("--out", default=str(TEAM_ROOT / "runs" / "check_audio_tts.wav"))
    ap.add_argument("--play", action="store_true", help="play the TTS wav (Windows winsound)")
    ap.add_argument("--env-file", default=".env")
    a = ap.parse_args()
    if not a.stt and not a.tts:
        ap.error("give --stt WAV and/or --tts [TEXT]")
    loaded = load_env_file(a.env_file) if a.env_file else []
    print(f"names loaded from {a.env_file!r}: {sorted(loaded) or '-'}")
    cfg = Config.from_env()
    if a.stt:
        print("STT", json.dumps(asyncio.run(check_stt(cfg, a.stt)), ensure_ascii=False))
    if a.tts:
        r = asyncio.run(check_tts(cfg, a.tts, Path(a.out)))
        print("TTS", json.dumps(r, ensure_ascii=False))
        if a.play:
            try:
                import winsound
                winsound.PlaySound(r["wav"], winsound.SND_FILENAME)
            except ImportError:
                print("no winsound here: open", r["wav"], "in a player")


if __name__ == "__main__":
    main()
