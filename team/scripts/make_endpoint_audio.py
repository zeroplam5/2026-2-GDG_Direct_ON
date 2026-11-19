"""맥 기본 음성(say, Yuna)으로 한국어 테스트 음성을 만든다. (v2)

실행 (스타터 키트 폴더에서, 맥에서만 동작):
  python team/scripts/make_endpoint_audio.py

만드는 것:
  team/data/endpoint_ko/audio/KO1.wav ~ KO9.wav     깨끗한 기계 음성 (16kHz, 16-bit, mono)
  team/data/endpoint_ko/audio/KO1N.wav ~ KO9N.wav   전화 음질 + 잡음 버전 (N = noisy)
  team/data/endpoint_ko/audio/meta.json          각 음성에서 말이 끝나는 시각
  team/data/endpoint_ko/cases.jsonl  측정기용 문항 목록

[[slnc 1200]] 은 1.2초 쉬기.
"""
import json, subprocess, wave
from pathlib import Path
import numpy as np
from scipy.signal import butter, sosfilt, resample_poly

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "team" / "data" / "endpoint_ko" / "audio"
OUT.mkdir(parents=True, exist_ok=True)
RATE = 16000

HIST3 = [{"role": "customer", "text": "주문번호 공일공 삼사오 취소해주세요."},
         {"role": "agent", "text": "네, 공일공 삼사오 주문 취소 요청 접수했어요."}]

# (번호, 문장, 이전 대화, 무엇을 시험하나)
CASES = [
    ("KO1", "주문번호 공일공 삼사오인데요, 언제 도착하나요?", [], "기본 배송 조회"),
    ("KO2", "어제 주문 취소했는데 아직 환불이 안 들어왔어요. 언제 들어와요?", [], "긴 문장, 두 문장"),
    ("KO3", "아까 그거 취소된 거 맞아요?", HIST3, "짧은 문장, 이전 대화 필요"),
    ("KO4", "어제부터 이 제품 먹었는데 속이 좀 안 좋고 두드러기가 났어요.", [], "인계 상황"),
    ("KO5", "주문번호가 [[slnc 1200]] 공일공 삼사오예요. 배송 어디쯤 왔는지 알려주세요.", [], "번호 앞에서 1.2초 쉼"),
    ("KO6", "주문번호 공일공 삼사오, 아 아니다, 공일공 삼사육이요. 배송 조회해주세요.", [], "번호 정정"),
    ("KO7", "배송 언제 와요? [[slnc 1500]] 아 그리고 주문번호는 공일공 삼사오예요.", [], "문장이 끝난 뒤 1.5초 쉬고 이어 말함"),
    ("KO8", "네, 취소요.", HIST3, "아주 짧게 바로 끝남"),
    ("KO9", "주문번호 공일공 [[slnc 1000]] 삼사오 [[slnc 1000]] 육칠팔이요.", [], "번호를 끊어서 부름 (1초씩 두 번)"),
]


def read_wav(path):
    with wave.open(str(path)) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)


def write_wav(path, a):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(np.clip(a, -32768, 32767).astype(np.int16).tobytes())


def speech_end(a, thresh=300):
    frame, last = 320, 0  # 20ms
    for i in range(0, len(a) - frame, frame):
        if np.sqrt(np.mean(a[i:i + frame] ** 2)) > thresh:
            last = i + frame
    return round(last / RATE, 3)


def phone_noisy(a, seed=0):
    """전화 음질 흉내: 8kHz로 낮췄다 올리기 + 300~3400Hz 대역만 + 잡음"""
    a = resample_poly(resample_poly(a, 1, 2), 2, 1)[: len(a)]
    sos = butter(4, [300, 3400], btype="bandpass", fs=RATE, output="sos")
    a = sosfilt(sos, a)
    rng = np.random.default_rng(seed)
    return a + rng.normal(0, 250, len(a))          # 잡음 크기(RMS) 약 250


meta, lines = {}, []
for i, (cid, text, history, note) in enumerate(CASES):
    wav = OUT / f"{cid}.wav"
    subprocess.run(["say", "-v", "Yuna", "-o", str(wav),
                    "--file-format=WAVE", "--data-format=LEI16@16000", text], check=True)
    clean = read_wav(wav)
    end = speech_end(clean)
    write_wav(OUT / f"{cid}N.wav", phone_noisy(clean, seed=i))
    plain = text.replace("[[slnc 1000]] ", "").replace("[[slnc 1200]] ", "").replace("[[slnc 1500]] ", "")
    for suffix in ("", "N"):
        meta[cid + suffix] = {"speech_end": end, "note": note + (" (전화 음질+잡음)" if suffix else "")}
        lines.append({"case_id": cid + suffix, "room_id": cid + suffix, "now": "2026-11-21 11:00",
                      "history": history, "customer_utterance": plain, "customer_profile": None})
    print(f"{cid}: 말 끝 {end}초  |  {note}  |  {plain}")

(OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
with open(ROOT / "team" / "data" / "endpoint_ko" / "cases.jsonl", "w", encoding="utf-8") as f:
    for l in lines:
        f.write(json.dumps(l, ensure_ascii=False) + "\n")
print(f"\n완료: 음성 {len(lines)}개 (깨끗 {len(CASES)} + 잡음 {len(CASES)}) → team/data/endpoint_ko/")
