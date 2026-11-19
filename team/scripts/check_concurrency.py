"""Concurrency check: does the agent behave the same with 4 simultaneous calls as with 1?
(The official measurement sends 4 cases at once.)

    # no API key: test doubles (fake STT by case_id, scripted LLM, tone TTS)
    .venv/Scripts/python -m team.scripts.check_concurrency --out team/runs/concurrency_fake
    # configured real providers (uses .env / AGENT_* as the agent would)
    .venv/Scripts/python -m team.scripts.check_concurrency --real --out team/runs/concurrency_real

For each level (default 1 and 4) the UNMODIFIED evaluator runs the practice set --repeats times
against ONE agent process. Reported per level: interruptions, errors, missing turn.end,
first sound / turn.end p50·p90, and whether every reply equals the level-1 reply for the same
case (a different reply under load = sessions leaking into each other or dropped work).
With test doubles the replies are deterministic, so any mismatch is a bug; with real models
replies vary, so the mismatch count is only informative.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from team.scripts.bench import REPO, start_agent


def pct(xs, q):
    xs = [x for x in xs if x is not None]
    return round(float(np.percentile(xs, q)), 2) if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="1,4")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--cases", default="team/data/practice_ko/cases.jsonl")
    ap.add_argument("--audio", default="team/data/practice_ko/audio")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--mock-port", type=int, default=8100)
    ap.add_argument("--real", action="store_true", help="use the configured providers instead of test doubles")
    ap.add_argument("--agent-env", default="", help="extra AGENT_* settings, comma separated")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = REPO / a.out
    out.mkdir(parents=True, exist_ok=True)

    env = dict(kv.split("=", 1) for kv in a.agent_env.split(",") if kv.strip())
    llm = "openai_compat"
    if not a.real:
        mapping = {}
        for line in open(REPO / a.cases, encoding="utf-8"):
            if line.strip():
                c = json.loads(line)  # practice sentences carry "/ <pause 1.3s> /" markers
                mapping[c["case_id"]] = re.sub(r"\s*/\s*<pause[^>]*>\s*/\s*", " ", c.get("customer_utterance", ""))
        (out / "fake_stt.json").write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        # fake STT returns the full sentence at once, which would make the completeness check commit
        # inside a pause; silence-only end-of-utterance keeps the comparison about concurrency
        env = {"AGENT_STT_PROVIDER": "fake", "AGENT_STT_FAKE_TEXT": str(out / "fake_stt.json"),
               "AGENT_TTS_PROVIDER": "tone", "AGENT_SEMANTIC_EOU": "0", **env}
        llm = "scripted"

    mock = subprocess.Popen([sys.executable, "-m", "team.mock.mock_api", "--port", str(a.mock_port)], cwd=REPO,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    agent = start_agent(env, a.port, out / "agent_metrics.jsonl", out / "agent.log", llm)
    levels = [int(x) for x in a.levels.split(",")]
    runs: dict[int, list[dict]] = {}
    try:
        for lv in levels:
            runs[lv] = []
            for r in range(a.repeats):
                d = out / f"c{lv}" / f"rep{r}"
                subprocess.run([sys.executable, "eval/run_eval.py", "--server", f"ws://127.0.0.1:{a.port}",
                                "--cases", a.cases, "--audio", a.audio, "--mock", f"http://127.0.0.1:{a.mock_port}",
                                "--out", str(d), "--concurrency", str(lv)], cwd=REPO, check=True,
                               env=dict(os.environ, PYTHONUTF8="1"), stdout=subprocess.DEVNULL)
                runs[lv] += [json.loads(l) for l in open(d / "results.jsonl", encoding="utf-8")]
    finally:
        agent.terminate(); agent.wait(10)
        mock.terminate(); mock.wait(10)

    base = {}
    for x in runs[levels[0]]:
        base.setdefault(x["case_id"], set()).add(x.get("agent_text", ""))
    summary, lines = {}, []
    head = "| 동시 연결 | 문항×회 | 끼어듦 | 오류 | turn.end 누락 | 첫 소리 p50 / p90 | 응답 완료 p50 / p90 | 답 내용이 1개씩일 때와 다름 |"
    lines += [head, "|" + "---|" * 8]
    for lv in levels:
        rs = runs[lv]
        ok = [x for x in rs if "error" not in x and not x.get("spoke_before_end")]
        mism = sum(1 for x in rs if x.get("agent_text", "") not in base.get(x["case_id"], set()))
        s = {"n": len(rs), "interruptions": sum(1 for x in rs if x.get("spoke_before_end")),
             "errors": sum(1 for x in rs if "error" in x),
             "missing_turn_end": sum(1 for x in rs if x.get("turn_end") is None),
             "first_sound": [pct([x["first_sound"] for x in ok], 50), pct([x["first_sound"] for x in ok], 90)],
             "turn_end": [pct([x["turn_end"] for x in ok], 50), pct([x["turn_end"] for x in ok], 90)],
             "reply_mismatch_vs_level1": mism}
        summary[lv] = s
        lines.append(f"| {lv} | {s['n']} | {s['interruptions']} | {s['errors']} | {s['missing_turn_end']} | "
                     f"{s['first_sound'][0]} / {s['first_sound'][1]} s | {s['turn_end'][0]} / {s['turn_end'][1]} s | {mism} |")
    per = ["", "| 문항 | " + " | ".join(f"동시 {lv} 첫 소리 p50" for lv in levels) + " |", "|---|" + "---|" * len(levels)]
    for cid in sorted(base):
        cells = []
        for lv in levels:
            v = [x["first_sound"] for x in runs[lv] if x["case_id"] == cid and x.get("first_sound") is not None
                 and not x.get("spoke_before_end")]
            cells.append(f"{pct(v, 50)} s" if v else "끼어듦/오류")
        per.append(f"| {cid} | " + " | ".join(cells) + " |")
    md = "\n".join(lines + per) + "\n"
    (out / "summary.json").write_text(json.dumps({"mode": "real" if a.real else "test_doubles", "env": env,
                                                   "levels": summary}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "report.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
