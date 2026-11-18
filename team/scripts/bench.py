"""Repeat the UNMODIFIED starter-kit evaluator (eval/run_eval.py) against the agent under
several end-of-utterance configurations and summarise latency / interruptions.

    .venv/Scripts/python -m team.scripts.bench --repeats 3 --out team/runs/bench1

* the practice mock server must already run on --mock
* each config starts its own agent process (env vars AGENT_*), on --port
* evaluator numbers: first_sound / turn_end relative to meta.json speech_end
* agent numbers (team metrics): stage timings relative to the arrival of the last voiced
  frame, so they are independent of meta.json accuracy
This is NOT an official score; the speed table in score_demo.py is a demo.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]

CONFIGS = {
    # starter-kit example server rule: RMS>=500 energy VAD, 800 ms silence, no transcript check
    "example_rule_800": {"AGENT_VAD": "energy", "AGENT_ENERGY_MIN_RMS": "500", "AGENT_SEMANTIC_EOU": "0",
                         "AGENT_MAX_SILENCE_MS": "800"},
    "semantic_700_1600": {"AGENT_COMPLETE_SILENCE_MS": "700", "AGENT_MAX_SILENCE_MS": "1600"},
    "default_1100_1800": {},
    "silence_only_1500": {"AGENT_SEMANTIC_EOU": "0", "AGENT_MAX_SILENCE_MS": "1500"},
}


def pct(xs, q):
    xs = [x for x in xs if x is not None]
    return round(float(np.percentile(xs, q)), 3) if xs else None


def start_agent(env_over, port, metrics, log_path, llm):
    env = dict(os.environ, **env_over, AGENT_METRICS_PATH=str(metrics), AGENT_LOG_TEXT="1")
    cmd = [sys.executable, "-m", "team.agent.server", "--port", str(port), "--llm", llm]
    logf = open(log_path, "w", encoding="utf-8")
    p = subprocess.Popen(cmd, cwd=REPO, env=env, stdout=logf, stderr=subprocess.STDOUT)
    for _ in range(120):
        time.sleep(0.5)
        if "agent listening" in Path(log_path).read_text(encoding="utf-8", errors="ignore"):
            return p
        if p.poll() is not None:
            break
    p.kill()
    raise RuntimeError("agent did not start: see " + str(log_path))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", default=",".join(CONFIGS))
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--cases", default="team/data/practice_ko/cases.jsonl")
    ap.add_argument("--audio", default="team/data/practice_ko/audio")
    ap.add_argument("--mock", default="http://127.0.0.1:8100")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--llm", default="scripted")
    ap.add_argument("--agent-env", default="", help="extra AGENT_* settings for every config, e.g. "
                    "AGENT_LLM_BASE_URL=https://api.openai.com/v1,AGENT_LLM_MODEL=gpt-4.1-mini,AGENT_LLM_API_KEY_ENV=OPENAI_API_KEY")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    extra = dict(kv.split("=", 1) for kv in a.agent_env.split(",") if kv.strip())
    for k in CONFIGS:
        CONFIGS[k] = {**CONFIGS[k], **extra}
    out = REPO / a.out
    out.mkdir(parents=True, exist_ok=True)
    summary = {}
    for name in a.configs.split(","):
        cdir = out / name
        cdir.mkdir(exist_ok=True)
        metrics = cdir / "agent_metrics.jsonl"
        proc = start_agent(CONFIGS[name], a.port, metrics, cdir / "agent.log", a.llm)
        try:
            for r in range(a.repeats):
                subprocess.run([sys.executable, "eval/run_eval.py", "--server", f"ws://127.0.0.1:{a.port}",
                                "--cases", a.cases, "--audio", a.audio, "--mock", a.mock,
                                "--out", str(cdir / f"rep{r}")], cwd=REPO, check=True,
                               env=dict(os.environ, PYTHONUTF8="1"), stdout=subprocess.DEVNULL)
        finally:
            proc.terminate()
            proc.wait(10)
        res = [json.loads(l) for r in range(a.repeats)
               for l in open(cdir / f"rep{r}" / "results.jsonl", encoding="utf-8")]
        met = [json.loads(l) for l in open(metrics, encoding="utf-8")]
        ok = [x for x in res if "error" not in x and not x.get("spoke_before_end")]
        per_case = {}
        for x in res:
            per_case.setdefault(x["case_id"], []).append(
                "INT" if x.get("spoke_before_end") else ("ERR" if "error" in x else round(x["first_sound"], 2) if x.get("first_sound") is not None else None))

        def stage(key_a, key_b=None):
            vals = []
            for m in met:
                va = m.get(key_a)
                vb = m.get(key_b) if key_b else 0
                if va is not None and vb is not None:
                    vals.append(va - vb)
            return {"p50": pct(vals, 50), "p90": pct(vals, 90), "n": len(vals)}

        summary[name] = {
            "env": CONFIGS[name], "n": len(res),
            "interruptions": sum(1 for x in res if x.get("spoke_before_end")),
            "errors": sum(1 for x in res if "error" in x),
            "missing_turn_end": sum(1 for x in res if x.get("turn_end") is None),
            "eval_first_sound_s": {"p50": pct([x["first_sound"] for x in ok], 50), "p90": pct([x["first_sound"] for x in ok], 90), "n": len(ok)},
            "eval_turn_end_s": {"p50": pct([x["turn_end"] for x in ok], 50), "p90": pct([x["turn_end"] for x in ok], 90)},
            "agent_ms": {
                "eou_commit": stage("commit_ms_rel"),
                "stt_compute": stage("stt_ms"),
                "stt_wait_after_commit": {"p50": pct([max(0, m["stt_end_ms_rel"] - m["commit_ms_rel"]) for m in met if m.get("stt_end_ms_rel") is not None], 50),
                                          "p90": pct([max(0, m["stt_end_ms_rel"] - m["commit_ms_rel"]) for m in met if m.get("stt_end_ms_rel") is not None], 90)},
                "llm_first_text_after_commit": stage("llm_first_text_ms_rel", "commit_ms_rel"),
                "first_audio_after_commit": stage("first_audio_ms_rel", "commit_ms_rel"),
                "first_audio_total": stage("first_audio_ms_rel"),
                "turn_end_total": stage("turn_end_ms_rel"),
                "tool_round_ms": {"p50": pct([t["ms"] for m in met for t in (m.get("tool_calls") or [])], 50),
                                  "p90": pct([t["ms"] for m in met for t in (m.get("tool_calls") or [])], 90)},
                "tts_first_sentence_ms": {"p50": pct([m["tts_ms"][0] for m in met if m.get("tts_ms")], 50),
                                          "p90": pct([m["tts_ms"][0] for m in met if m.get("tts_ms")], 90)},
            },
            "per_case_first_sound": per_case,
            "eou_reasons": {k: sum(1 for m in met if m.get("eou_reason") == k) for k in {m.get("eou_reason") for m in met}},
        }
        print(name, json.dumps({k: summary[name][k] for k in ("n", "interruptions", "errors", "eval_first_sound_s")}, ensure_ascii=False), flush=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
