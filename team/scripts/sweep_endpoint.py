"""말 끝 판단 설정을 바꿔가며 한국어 테스트 음성 전체를 자동으로 측정한다.

결과: 설정별 조기 응답(끼어들기) 횟수와 첫 음성 시간 표

실행 (스타터 키트 폴더에서):
  python team/scripts/sweep_endpoint.py                       # 키 없이: 침묵 길이 500/800/1000/1300/1500ms 비교 (--dry)
  python team/scripts/sweep_endpoint.py --values 800 1300     # 원하는 값만

  # 토요일, 키가 있을 때: Soniox max_endpoint_delay_ms 비교
  python team/scripts/sweep_endpoint.py --soniox --values 500 1000 1500

결과 파일:
  team/runs/endpoint_sweep_<이름>/summary.md   표 (팀 공유용)
  team/runs/endpoint_sweep_<이름>/cases.csv    문항별 원자료
"""
import argparse, csv, json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TABLE = [(1.5, 100), (2.0, 90), (2.5, 80), (3.0, 70), (3.5, 60), (4.0, 50), (5.0, 30), (6.0, 10)]


def speed_pts(t):
    if t is None:
        return 0
    return next((v for b, v in TABLE if t <= b), 0)


def wait_port(proc, word, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        line = proc.stdout.readline()
        if word in line:
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--values", type=int, nargs="+", default=[500, 800, 1000, 1300, 1500])
    ap.add_argument("--soniox", action="store_true", help="키 사용: Soniox MAX_ENDPOINT_DELAY_MS 비교")
    ap.add_argument("--cases", default="team/data/endpoint_ko/cases.jsonl")
    ap.add_argument("--audio", default="team/data/endpoint_ko/audio")
    a = ap.parse_args()

    name = "soniox" if a.soniox else "dry"
    stamp = time.strftime("%m%d_%H%M")
    base = ROOT / "team" / "runs" / f"endpoint_sweep_{name}_{stamp}"
    base.mkdir(parents=True, exist_ok=True)
    meta = json.load(open(ROOT / a.audio / "meta.json", encoding="utf-8"))

    mock = subprocess.Popen([sys.executable, "-u", "mock_tools/mock_api_template.py", "--port", "8199"],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1)

    rows, per_case = [], {}
    try:
        for v in a.values:
            env = dict(os.environ)
            if a.soniox:
                env["MAX_ENDPOINT_DELAY_MS"] = str(v)
                cmd = [sys.executable, "-u", "team/scripts/endpoint_server.py", "--port", "9199"]
            else:
                env["DRY_END_MS"] = str(v)
                cmd = [sys.executable, "-u", "team/scripts/endpoint_server.py", "--port", "9199", "--dry"]
            srv = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
            if not wait_port(srv, "voice server on"):
                srv.kill()
                raise SystemExit("음성 서버가 안 켜졌어요.")
            out = base / f"{v}ms"
            print(f"\n=== 설정 {v}ms 측정 중 ===", flush=True)
            subprocess.run([sys.executable, "eval/run_eval.py", "--server", "ws://127.0.0.1:9199",
                            "--cases", a.cases, "--audio", a.audio, "--mock", "http://127.0.0.1:8199",
                            "--out", str(out)], cwd=ROOT, check=True)
            srv.terminate(); srv.wait()

            res = [json.loads(l) for l in open(out / "results.jsonl", encoding="utf-8")]
            early = [r["case_id"] for r in res if r.get("spoke_before_end")]
            ok = [r["first_sound"] for r in res if not r.get("spoke_before_end") and r.get("first_sound") is not None]
            pts = [0 if r.get("spoke_before_end") else speed_pts(r.get("first_sound")) for r in res]
            rows.append({"설정(ms)": v, "문항 수": len(res), "조기 응답": len(early),
                         "조기 응답 문항": ", ".join(sorted(early)) or "-",
                         "평균 첫 음성(초)": round(sum(ok) / len(ok), 2) if ok else "-",
                         "최대 첫 음성(초)": round(max(ok), 2) if ok else "-",
                         "평균 속도 점수": round(sum(pts) / len(pts), 1) if pts else 0})
            for r in res:
                per_case.setdefault(r["case_id"], {})[v] = (
                    "끼어듦" if r.get("spoke_before_end") else
                    (f"{r['first_sound']:.2f}" if r.get("first_sound") is not None else "소리 없음"))
    finally:
        mock.terminate()

    cols = list(rows[0].keys())
    md = [f"# 말 끝 설정 비교 ({'Soniox max_endpoint_delay_ms' if a.soniox else '침묵 길이, --dry'})", "",
          "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    md += ["| " + " | ".join(str(r[c]) for c in cols) + " |" for r in rows]
    md += ["", "## 문항별 첫 음성(초)", "",
           "| 문항 | 내용 | " + " | ".join(f"{v}ms" for v in a.values) + " |",
           "|" + "---|" * (len(a.values) + 2)]
    for cid in sorted(per_case):
        md.append(f"| {cid} | {meta.get(cid, {}).get('note', '')} | "
                  + " | ".join(per_case[cid].get(v, "-") for v in a.values) + " |")
    (base / "summary.md").write_text("\n".join(md), encoding="utf-8")

    with open(base / "cases.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["문항", "내용"] + [f"{v}ms" for v in a.values])
        for cid in sorted(per_case):
            w.writerow([cid, meta.get(cid, {}).get("note", "")] + [per_case[cid].get(v, "-") for v in a.values])

    print("\n" + "\n".join(md))
    print(f"\n저장: {base}/summary.md, cases.csv")


if __name__ == "__main__":
    main()
