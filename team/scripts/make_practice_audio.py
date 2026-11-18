"""Generate TEAM PRACTICE Korean test audio with the local SAPI voice (not official data).

Output (starter-kit evaluator format):
    team/data/practice_ko/cases.jsonl
    team/data/practice_ko/audio/{case_id}.wav + meta.json (speech_end measured from the audio)

Limitations: a clean synthetic voice is far easier for STT than real callers; pauses are
inserted digital silence. Use this for protocol / latency / endpointing checks only.

    .venv/Scripts/python -m team.scripts.make_practice_audio
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np

from team.agent.config import Config, TEAM_ROOT
from team.agent.providers.tts import SapiTTS

OUT = TEAM_ROOT / "data" / "practice_ko"
RATE = 16000

# (case_id, parts, caller, note). parts: str = speech, float = pause seconds
CASES = [
    ("K01", ["주문번호는 데모 일 공 공 일 이에요. 배송이 어디쯤인지 알려주세요."], None, "order found"),
    ("K02", ["주문번호 데모 구 구 구 구 조회해 주세요."], None, "order not found"),
    ("K03", ["어제 주문했는데", 0.5, "아직 배송이 안 왔어요. 주문번호는 데모 이 공 공 이 예요."], None, "short pause 0.5 s"),
    ("K04", ["주문번호가", 1.3, "데모 일 공 공 일 이에요. 배송 상태 알려주세요."], None, "long pause 1.3 s after incomplete clause"),
    ("K05", ["배송 문의 좀 드릴게요.", 1.3, "주문번호는 데모 이 공 공 이 입니다."], None, "long pause 1.3 s after complete-looking sentence (known risk)"),
    ("K06", ["어제부터 이 제품을 먹고 두드러기가 났어요. 상담원이랑 통화하고 싶어요."], None, "health issue + human request"),
    ("K07", ["내일 오전에 다시 전화 주실 수 있나요?"],
     {"name": "연습 고객", "phone": "01000001234", "email": None}, "callback, caller known"),
    ("K08", ["주문번호 데모 일 공 공 일, 아 아니다, 데모 이 공 공 이 요. 배송 상태 알려주세요."], None, "self-correction"),
]


def voiced_end(pcm: bytes, thr: float = 300.0) -> float:
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = len(a) // 320
    e = np.sqrt((a[: n * 320].reshape(n, 320) ** 2).mean(1))
    idx = np.nonzero(e >= thr)[0]
    return float((idx[-1] + 1) * 0.02) if len(idx) else 0.0


def trim(pcm: bytes, thr=200.0) -> bytes:
    a = np.frombuffer(pcm, dtype=np.int16)
    n = len(a) // 160
    e = np.sqrt((a[: n * 160].astype(np.float32).reshape(n, 160) ** 2).mean(1))
    idx = np.nonzero(e >= thr)[0]
    if not len(idx):
        return b""
    return a[idx[0] * 160:(idx[-1] + 1) * 160].tobytes()


def main():
    cfg = Config.from_env(tts_rate=0, tts_workers=1)
    tts = SapiTTS(cfg)
    (OUT / "audio").mkdir(parents=True, exist_ok=True)
    meta, rows = {}, []
    for cid, parts, caller, note in CASES:
        pcm = b"\0" * int(RATE * 0.4 * 2)  # 0.4 s lead-in silence
        for p in parts:
            if isinstance(p, float):
                pcm += b"\0" * (int(RATE * p) * 2)
            else:
                pcm += trim(tts.pool.submit(tts._run, p).result())
        end = voiced_end(pcm)
        pcm += b"\0" * int(RATE * 0.3 * 2)
        with wave.open(str(OUT / "audio" / f"{cid}.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(pcm)
        meta[cid] = {"speech_end": round(end, 3)}
        rows.append({"case_id": cid, "room_id": cid, "now": "2026-11-21 11:00", "history": [],
                     "customer_utterance": " / ".join(p if isinstance(p, str) else f"<pause {p}s>" for p in parts),
                     "customer_profile": caller, "_team_practice_only": True, "note": note})
        print(cid, f"{len(pcm) / 32000:.2f}s speech_end={end:.2f}s", note)
    (OUT / "audio" / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    with open(OUT / "cases.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
