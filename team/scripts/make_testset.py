"""Turn human recordings into a starter-kit evaluator test set (team format, like practice_ko).

    .venv/Scripts/python -m team.scripts.make_testset team/data/holdout_ko/recordings \
        --name holdout_ko --script team/data/holdout_ko/script.tsv --variants phone,noise

Input files: <ID>.<ext> or <ID>_<speaker>.<ext> (wav, m4a, mp3 ... anything ffmpeg reads; without
ffmpeg only 16-bit wav). With several speakers per ID the case ids become H01A, H01B ...
Output: team/data/<name>/audio/<CASE>.wav (16 kHz s16le mono), audio/meta.json, cases.jsonl

speech_start / speech_end are found from the frame energy (noise floor + loudness), the audio is
trimmed to --lead s before / --tail s after speech so no trailing noise is left (the starter-kit
demo audio had seconds of sound after its speech_end). ALWAYS listen and correct meta.json if needed.

Variants (generated from the same recording, same speech_end):
  phone: 300-3400 Hz band + 8 kHz round trip (telephone quality)  -> case id + "P"
  noise: background noise at --snr dB                               -> case id + "N"
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import wave
from math import gcd
from pathlib import Path

import numpy as np
from scipy.signal import butter, resample_poly, sosfiltfilt

from team.agent.config import TEAM_ROOT

RATE = 16000
HOP = 320  # 20 ms
EXTS = {".wav", ".m4a", ".mp3", ".aac", ".ogg", ".opus", ".flac", ".webm", ".mp4", ".caf", ".3gp", ".amr"}


def load_16k(path: Path) -> np.ndarray:
    if shutil.which("ffmpeg"):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
                             check=True, capture_output=True).stdout
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if path.suffix.lower() != ".wav":
        sys.exit(f"ffmpeg 이 없어 {path.name} 을 읽을 수 없습니다 (16bit wav 만 가능)")
    with wave.open(str(path)) as w:
        if w.getsampwidth() != 2:
            sys.exit(f"{path.name}: 16bit wav 만 지원")
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
        if w.getnchannels() > 1:
            x = x.reshape(-1, w.getnchannels()).mean(1)
        if w.getframerate() != RATE:
            g = gcd(RATE, w.getframerate())
            x = resample_poly(x, RATE // g, w.getframerate() // g)
    return x


def frame_rms(x: np.ndarray) -> np.ndarray:
    n = len(x) // HOP
    f = x[: n * HOP].reshape(n, HOP)
    return np.sqrt((f * f).mean(1))


def speech_runs(r: np.ndarray, min_run: int = 3):
    """Runs of >= 60 ms above max(3 x noise floor, 12 % of the loud level)."""
    noise = max(float(np.percentile(r, 10)), 20.0)
    thr = max(noise * 3.0, float(np.percentile(r, 95)) * 0.12)
    on = r >= thr
    runs, i = [], 0
    while i < len(on):
        if on[i]:
            j = i
            while j < len(on) and on[j]:
                j += 1
            if j - i >= min_run:
                runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def phone(x: np.ndarray) -> np.ndarray:
    y = sosfiltfilt(butter(4, [300, 3400], btype="band", fs=RATE, output="sos"), x)
    return resample_poly(resample_poly(y, 1, 2), 2, 1)[: len(x)]


def add_noise(x: np.ndarray, s0: int, s1: int, snr_db: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = sosfiltfilt(butter(2, 2500, btype="low", fs=RATE, output="sos"), rng.standard_normal(len(x)))
    sp = x[s0:s1]
    p_sig = float((sp * sp).mean()) if len(sp) else float((x * x).mean())
    n *= np.sqrt(p_sig / (10 ** (snr_db / 10)) / max(float((n * n).mean()), 1e-9))
    return x + n


def write_wav(path: Path, x: np.ndarray):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.clip(np.round(x), -32768, 32767).astype(np.int16).tobytes())


def read_script(path: str | None) -> dict:
    if not path:
        return {}
    rows = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.reader(f, delimiter="\t"):
            if not r or r[0] == "id":
                continue
            r += [""] * (5 - len(r))
            text = re.sub(r"\s*\|\s*\([^)]*\)\s*\|\s*", " ", r[2]).strip()
            rows[r[0]] = {"condition": r[1], "utterance": text, "caller_phone": r[3].strip(), "expected": r[4]}
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="folder with recordings (or one file)")
    ap.add_argument("--name", required=True, help="output folder under team/data/")
    ap.add_argument("--script", default=None, help="script.tsv (id, 조건, 문장, caller_phone, 기대 동작)")
    ap.add_argument("--lead", type=float, default=0.5)
    ap.add_argument("--tail", type=float, default=1.0)
    ap.add_argument("--peak", type=float, default=16000, help="normalize peak to this (0 = keep level)")
    ap.add_argument("--variants", default="", help="phone,noise")
    ap.add_argument("--variant-ids", default="H01,H02,H03,H04,H08")
    ap.add_argument("--snr", type=float, default=15.0)
    ap.add_argument("--pause", type=float, default=0.6, help="report pauses at least this long")
    ap.add_argument("--now", default="2026-11-21 11:00")
    a = ap.parse_args()

    src = Path(a.src)
    files = sorted([src] if src.is_file() else [p for p in src.iterdir() if p.suffix.lower() in EXTS])
    if not files:
        sys.exit(f"{src} 에 음성 파일이 없습니다")
    script = read_script(a.script)
    by_id: dict[str, list[tuple[str, Path]]] = {}
    for f in files:
        base, _, speaker = f.stem.partition("_")
        by_id.setdefault(base, []).append((speaker, f))
    variants = [v.strip() for v in a.variants.split(",") if v.strip()]
    variant_ids = {v.strip() for v in a.variant_ids.split(",") if v.strip()}

    out = TEAM_ROOT / "data" / a.name
    audio = out / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    meta, cases = {}, []
    print(f"{'문항':<8}{'원본':<26}{'길이':>7}{'말 시작':>8}{'말 끝':>7}  중간 쉼")
    for base in sorted(by_id):
        group = sorted(by_id[base])
        for k, (speaker, f) in enumerate(group):
            cid = base + (chr(ord("A") + k) if len(group) > 1 else "")
            x = load_16k(f)
            runs = speech_runs(frame_rms(x))
            if not runs:
                print(f"  {f.name}: 말소리를 못 찾음 → 건너뜀")
                continue
            s, e = runs[0][0] * HOP, runs[-1][1] * HOP
            a0, a1 = max(0, s - int(a.lead * RATE)), min(len(x), e + int(a.tail * RATE))
            y = x[a0:a1]
            if a.peak:
                y = y * (a.peak / max(1.0, float(np.abs(y).max())))
            s_rel, e_rel = s - a0, e - a0
            pauses = [(round((p1 * HOP - a0) / RATE, 2), round((n0 - p1) * HOP / RATE, 2))
                      for (_, p1), (n0, _) in zip(runs, runs[1:]) if (n0 - p1) * HOP / RATE >= a.pause]
            info = script.get(base, {})
            versions = [(cid, y, "원본")]
            if base in variant_ids:
                if "phone" in variants:
                    versions.append((cid + "P", phone(y), "전화 음질"))
                if "noise" in variants:
                    versions.append((cid + "N", add_noise(y, s_rel, e_rel, a.snr, seed=len(cases)), f"잡음 SNR {a.snr:g}dB"))
            for vid, vy, kind in versions:
                write_wav(audio / f"{vid}.wav", vy)
                meta[vid] = {"speech_end": round(e_rel / RATE, 3), "speech_start": round(s_rel / RATE, 3),
                             "source": f.name, "pauses": pauses, "variant": kind}
                profile = {"name": None, "phone": info["caller_phone"], "email": None} if info.get("caller_phone") else None
                cases.append({"case_id": vid, "room_id": vid, "now": a.now, "history": [],
                              "customer_utterance": info.get("utterance", ""), "customer_profile": profile,
                              "_team_holdout_only": True, "condition": info.get("condition", ""),
                              "expected": info.get("expected", ""), "variant": kind, "speaker": speaker or None})
            ps = ", ".join(f"{t}s부터 {d}s" for t, d in pauses) or "-"
            print(f"{cid:<8}{f.name[:24]:<26}{len(y) / RATE:>6.2f}s{s_rel / RATE:>7.2f}s{e_rel / RATE:>6.2f}s  {ps}"
                  + (f"  (+{', '.join(v[2] for v in versions[1:])})" if len(versions) > 1 else ""))

    (audio / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "cases.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8")
    missing = sorted(set(script) - set(by_id))
    print(f"\n{len(cases)}문항 → team/data/{a.name}/audio/, team/data/{a.name}/cases.jsonl")
    if missing:
        print("녹음이 없는 문항:", ", ".join(missing))


if __name__ == "__main__":
    main()
