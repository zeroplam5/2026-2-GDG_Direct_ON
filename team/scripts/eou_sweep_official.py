"""End-of-utterance settings on the OFFICIAL practice voices, with the OFFICIAL evaluator.

    python -m team.scripts.eou_sweep_official --official <공식 자료 폴더> --out <폴더 (저장소 밖 권장)>

For each setting one agent process is started (test doubles: fake STT that returns the case's
customer_utterance, scripted LLM, tone TTS - no API key), the official eval/run_eval.py sends all
practice voices (--concurrency 4, as in the official run) and the per-case first sound /
interruption is kept. Settings run in parallel on different ports.

Caveat: the fake STT returns the FULL sentence even while the customer pauses, so the
sentence-completeness check (semantic EOU) can commit earlier than with a real STT. Interruption
counts of the semantic settings are therefore an upper bound.
Only numbers are written (no customer text): results_<setting>.json = {case_id: [first_sound|"INT", after_commit_s, has_tool]}.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

SETTINGS = {
    "default_1100_1800": {},
    "semantic_700_1600": {"AGENT_COMPLETE_SILENCE_MS": "700", "AGENT_MAX_SILENCE_MS": "1600"},
    "semantic_600_2000": {"AGENT_COMPLETE_SILENCE_MS": "600", "AGENT_MAX_SILENCE_MS": "2000"},
    "silence_800": {"AGENT_SEMANTIC_EOU": "0", "AGENT_MAX_SILENCE_MS": "800"},
    "silence_1000": {"AGENT_SEMANTIC_EOU": "0", "AGENT_MAX_SILENCE_MS": "1000"},
    "silence_1300": {"AGENT_SEMANTIC_EOU": "0", "AGENT_MAX_SILENCE_MS": "1300"},
    "silence_1500": {"AGENT_SEMANTIC_EOU": "0", "AGENT_MAX_SILENCE_MS": "1500"},
}


def wait_port(port, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--official", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--settings", default=",".join(SETTINGS))
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    kit, out = Path(a.official).resolve(), Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    texts = {}
    for line in (kit / "data" / "practice_cases.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            c = json.loads(line)
            texts[c["case_id"]] = (c.get("customer_utterance", ""), bool((c.get("reference") or {}).get("tool_calls")))
    fake = out / "fake_stt.json"
    fake.write_text(json.dumps({k: v[0] for k, v in texts.items()}, ensure_ascii=False), encoding="utf-8")
    names = a.settings.split(",")
    procs, runs = [], []
    mock = subprocess.Popen([sys.executable, "-m", "team.mock.mock_api", "--port", "8100", "--db",
                             str(kit / "data" / "mock_db.json"), "--tools", str(kit / "data" / "brand" / "tools.json")],
                            cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(mock)
    try:
        wait_port(8100)
        for i, n in enumerate(names):
            env = {**os.environ, "AGENT_STT_PROVIDER": "fake", "AGENT_STT_FAKE_TEXT": str(fake),
                   "AGENT_LLM_PROVIDER": "scripted", "AGENT_TTS_PROVIDER": "tone", "AGENT_EOU_MODE": "vad",
                   "AGENT_METRICS_PATH": str(out / f"metrics_{n}.jsonl"), "AGENT_LOG_TEXT": "0", **SETTINGS[n]}
            (out / f"metrics_{n}.jsonl").unlink(missing_ok=True)
            procs.append(subprocess.Popen([sys.executable, "-m", "team.agent.server", "--port", str(9101 + i), "--env-file", ""],
                                          cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for i, n in enumerate(names):
            wait_port(9101 + i)
            cmd = [sys.executable, "run_eval.py", "--server", f"ws://127.0.0.1:{9101 + i}", "--mock", "http://127.0.0.1:8100",
                   "--relay-port", str(8301 + i), "--cases", "../data/practice_cases.jsonl", "--audio", "../data/audio/practice",
                   "--out", str(out / f"run_{n}")] + (["--limit", str(a.limit)] if a.limit else [])
            runs.append((n, subprocess.Popen(cmd, cwd=kit / "eval", stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)))
        for n, p in runs:
            _, err = p.communicate()
            if p.returncode:
                print(f"{n}: 측정기 오류 {err[-300:]}", file=sys.stderr)
        time.sleep(1)
    finally:
        for p in procs:
            p.terminate()
    for n in names:
        met = {}
        mp = out / f"metrics_{n}.jsonl"
        for line in (mp.read_text(encoding="utf-8").splitlines() if mp.exists() else []):
            m = json.loads(line)
            if m.get("first_audio_ms_rel") is not None and m.get("commit_ms_rel") is not None:
                met[m["session_id"]] = (m["first_audio_ms_rel"] - m["commit_ms_rel"]) / 1000
        res = {}
        rp = out / f"run_{n}" / "results.jsonl"
        for line in (rp.read_text(encoding="utf-8").splitlines() if rp.exists() else []):
            r = json.loads(line)
            fs = "INT" if r.get("spoke_before_end") else r.get("first_sound")
            res[r["case_id"]] = [fs, met.get(r["session_id"]), texts.get(r["case_id"], ("", False))[1]]
        (out / f"results_{n}.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        ints = sum(1 for v in res.values() if v[0] == "INT")
        print(f"{n}: 문항 {len(res)}, 끼어듦 {ints}, 오류/소리없음 {sum(1 for v in res.values() if v[0] is None)}")


if __name__ == "__main__":
    main()
