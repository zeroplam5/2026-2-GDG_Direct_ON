"""End-of-utterance cost table: which waiting rule gives the best expected SPEED score?

Idea (Raux & Eskenazi, FSTTM): choose the turn-taking rule by comparing the cost of cutting in
with the cost of waiting. In this contest both costs are on the speed table:
  * cutting in (first sound before the customer finished) -> that case's speed score is 0
  * waiting longer -> first sound later -> 100, 90, 80 ... by 0.5 s steps

    .venv/Scripts/python -m team.scripts.eou_cost_table            # default inputs below
    .venv/Scripts/python -m team.scripts.eou_cost_table --pipeline 1.0,1.5,2.08,2.63 --out team/docs/EOU_COST.md

first sound = (end-of-utterance wait, measured) + (STT -> LLM -> first TTS audio after the
decision, ASSUMED per column). The measured runs used test doubles (beep / scripted), so their
own pipeline time is subtracted first. Only the first-sound part of the speed score is modelled:
for function cases the official speed score is the average of first-sound and after-function
scores, and the after-function part does not depend on the end-of-utterance rule.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TABLE = [(1.5, 100), (2.0, 90), (2.5, 80), (3.0, 70), (3.5, 60), (4.0, 50), (5.0, 30), (6.0, 10)]
INT = "INT"


def pts(t):
    if t is None or t < 0:
        return 0
    return next((v for bound, v in TABLE if t <= bound), 0)


def load_sweep(path: Path):
    """Minsu's silence-only sweep (beep server): columns = wait in ms, '끼어듦' = cut in.
    Beep reply -> pipeline ~0, so the measured first sound is the wait itself."""
    rows = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))
    head, body = rows[0], rows[1:]
    out = {}
    for j, col in enumerate(head[2:], start=2):
        cases = {}
        for r in body:
            v = r[j].strip()
            cases[r[0]] = INT if v in ("끼어듦", "INT") else float(v)
        out[f"침묵 {int(col.removesuffix('ms')) / 1000:.1f}초"] = cases
    return out


def load_bench(path: Path, labels: dict):
    """team bench summary (scripted LLM + tone TTS): subtract the measured after-decision time."""
    d = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for name, v in d.items():
        off = (v["agent_ms"]["first_audio_after_commit"]["p50"] or 0) / 1000
        cases = {}
        for cid, xs in v["per_case_first_sound"].items():
            for i, x in enumerate(xs):
                cases[f"{cid}#{i}"] = INT if x == INT else max(0.0, x - off)
        out[labels.get(name, name)] = cases
    return out


def table(title: str, configs: dict, pipes: list[float]) -> list[str]:
    lines = [f"### {title}", "",
             "| 말 끝 규칙 | 문항 | 끼어듦 | 말 끝 판단 p50 | " + " | ".join(f"처리 {p:g}초일 때 평균 속도 점수" for p in pipes) + " |",
             "|---|---|---|---|" + "---|" * len(pipes)]
    best = {p: (-1, "") for p in pipes}
    for name, cases in configs.items():
        vals = list(cases.values())
        waits = sorted(x for x in vals if x != INT)
        n_int = sum(1 for x in vals if x == INT)
        p50 = waits[len(waits) // 2] if waits else None
        cells = []
        for p in pipes:
            s = sum(pts(x + p) if x != INT else 0 for x in vals) / len(vals)
            cells.append(s)
            if s > best[p][0]:
                best[p] = (s, name)
        lines.append(f"| {name} | {len(vals)} | {n_int} | {p50:.2f}초 | " if p50 is not None else f"| {name} | {len(vals)} | {n_int} | - | ")
        lines[-1] += " | ".join(f"{c:.1f}" for c in cells) + " |"
    lines += ["", "가장 높은 규칙: " + " / ".join(f"처리 {p:g}초 → **{best[p][1]}** ({best[p][0]:.1f})" for p in pipes), ""]
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", default="team/runs/endpoint_sweep_dry_1119/cases.csv")
    ap.add_argument("--bench", default="team/runs/bench_scripted/summary.json")
    ap.add_argument("--pipeline", default="1.0,1.5,2.08,2.63",
                    help="assumed seconds from end-of-utterance decision to first audio (STT+LLM+TTS)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    pipes = [float(x) for x in a.pipeline.split(",")]
    labels = {"example_rule_800": "음량 + 침묵 0.8초 (스타터킷 예시)",
              "semantic_700_1600": "문장 완결 0.7초 / 최대 1.6초",
              "default_1100_1800": "문장 완결 1.1초 / 최대 1.8초 (현재 기본값)",
              "silence_only_1500": "침묵만 1.5초"}
    md = ["# 말 끝 판단 비용표", ""]
    md += table("A. 말 끝 판단 실험 음성 18개 (team/data/endpoint_ko, 침묵만, 삐 소리)", load_sweep(REPO / a.sweep), pipes)
    md += table("B. 팀 연습 음성 8문항 × 3회 (team/data/practice_ko, 대체물)", load_bench(REPO / a.bench, labels), pipes)
    text = "\n".join(md)
    print(text)
    if a.out:
        Path(REPO / a.out).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
