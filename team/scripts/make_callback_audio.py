"""Generate the TEAM callback second-turn cases with the local SAPI voice (not official data).

Starter kit v2 DEMO3 ("내일 오전에 다시 전화 좀 부탁드려요." + caller info) ends after one turn,
and our gate (confirm: explicit) only asks the read-back question there. These cases start
from that question (history) so the evaluator can see the request_callback call and the
answer after its result:

    DEMO3C  plain consent                          -> request_callback(01000001234), no time promised
    DEMO3T  consent + a time the function can't keep -> same call; must not say the time was booked

Output (starter-kit evaluator format):
    team/data/callback_ko/cases.jsonl
    team/data/callback_ko/audio/{case_id}.wav + meta.json (speech_end measured from the audio)

    .venv/Scripts/python -m team.scripts.make_callback_audio
See team/docs/STARTER_V2_1121.md for the experiment these were made for.
"""
from __future__ import annotations

import json
import wave

from team.agent.config import Config, TEAM_ROOT
from team.agent.providers.tts import SapiTTS
from team.scripts.make_practice_audio import RATE, trim, voiced_end

OUT = TEAM_ROOT / "data" / "callback_ko"
CALLER = {"name": "데모 고객", "phone": "01000001234", "email": None}
# the agent line is the gate's confirm_template for request_callback (practice_rules.json)
HISTORY = [{"role": "customer", "text": "내일 오전에 다시 전화 좀 부탁드려요."},
           {"role": "agent", "text": "01000001234 번호로 상담원 연락을 접수해 드릴까요?"}]
CASES = [
    ("DEMO3C", "네, 그 번호로 해 주세요.",
     "consent -> request_callback(01000001234); no time promised"),
    ("DEMO3T", "네, 그 번호로 내일 오전 열 시쯤으로 부탁드려요.",
     "consent + wished time -> request_callback(01000001234); must not say the time was booked"),
]


def main():
    tts = SapiTTS(Config.from_env(tts_rate=0, tts_workers=1))
    (OUT / "audio").mkdir(parents=True, exist_ok=True)
    meta, rows = {}, []
    for cid, text, note in CASES:
        pcm = b"\0" * int(RATE * 0.4 * 2) + trim(tts.pool.submit(tts._run, text).result())  # 0.4 s lead-in
        end = voiced_end(pcm)
        pcm += b"\0" * int(RATE * 0.3 * 2)
        with wave.open(str(OUT / "audio" / f"{cid}.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(pcm)
        meta[cid] = {"speech_end": round(end, 3)}
        rows.append({"case_id": cid, "room_id": cid, "now": "2026-11-21 11:00", "history": HISTORY,
                     "customer_utterance": text, "customer_profile": CALLER, "_team_practice_only": True,
                     "note": note})
        print(cid, f"{len(pcm) / 32000:.2f}s speech_end={end:.2f}s", note)
    (OUT / "audio" / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    with open(OUT / "cases.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
