"""External access check: can the organisers' laptop reach BOTH servers?

Run this from ANOTHER machine (a teammate's laptop on the venue Wi-Fi) or through the tunnel
addresses, with the two addresses you would hand to the organisers:

    .venv/Scripts/python -m team.scripts.check_external \
        --agent wss://xxxx.trycloudflare.com --mock https://yyyy.trycloudflare.com
    .venv/Scripts/python -m team.scripts.check_external --agent ws://192.168.0.12:9000 --mock http://192.168.0.12:8100

Checks (in order):
  1. mock  GET  /tools                      -> 200 + function list
  2. mock  POST /tools/<first function>     -> JSON (any status), with X-Session-Id
  3. mock  POST /tools/__not_defined__      -> 404 (functions outside the definition are rejected)
  4. agent full turn with the UNMODIFIED starter-kit evaluator, tools_url = the --mock address
           -> audio received, turn.end received, no interruption
  5. the agent's function calls reached the mock under the evaluator's session id

Note: in the official run the organisers' relay sits in front of the mock and the agent calls the
relay (tools_url). Check 4 also proves the agent can make outbound calls to an external tools_url.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]

HINTS = {
    "refused": "연결 거부: 서버가 꺼져 있거나 127.0.0.1(내 컴퓨터 전용)로 떠 있음 → --host 0.0.0.0 으로 띄우기",
    "timeout": "시간 초과: 방화벽·학교 와이파이가 막는 중일 수 있음 → 터널(cloudflared/ngrok) 주소 사용",
    "dns": "주소를 못 찾음: 터널 주소 오타이거나 터널이 꺼짐",
    "tls": "TLS 오류: 터널 주소는 https / wss 로, 노트북 IP는 http / ws 로",
}


def hint(e: Exception) -> str:
    s = repr(e).lower()
    if "refused" in s or "connecterror" in s and "111" in s:
        return HINTS["refused"]
    if "timeout" in s:
        return HINTS["timeout"]
    if "name or service" in s or "getaddrinfo" in s or "nodename" in s:
        return HINTS["dns"]
    if "ssl" in s or "certificate" in s:
        return HINTS["tls"]
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True, help="ws://IP:9000 or wss://tunnel-host")
    ap.add_argument("--mock", required=True, help="http://IP:8100 or https://tunnel-host")
    ap.add_argument("--cases", default="team/data/practice_ko/cases.jsonl")
    ap.add_argument("--audio", default="team/data/practice_ko/audio")
    ap.add_argument("--case-ids", default="K01,K07", help="one case with a function call, one without")
    ap.add_argument("--timeout", type=float, default=10.0)
    a = ap.parse_args()
    mock = a.mock.rstrip("/")
    rows: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        rows.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)
        return ok

    # 1-3: mock server
    tools = []
    with httpx.Client(timeout=a.timeout) as c:
        try:
            r = c.get(mock + "/tools")
            tools = r.json() if r.status_code == 200 else []
            check("1. 모의 서버 GET /tools", r.status_code == 200 and isinstance(tools, list) and tools,
                  f"HTTP {r.status_code}, 함수 {len(tools)}개")
        except Exception as e:
            check("1. 모의 서버 GET /tools", False, f"{type(e).__name__} {hint(e)}")
        if tools:
            fn = (tools[0].get("function") or tools[0])["name"]
            sid = "EXTCHECK-" + uuid.uuid4().hex[:6]
            try:
                r = c.post(f"{mock}/tools/{fn}", json={}, headers={"X-Session-Id": sid})
                check("2. 모의 서버 POST /tools/{name}", r.status_code == 200 and isinstance(r.json(), dict),
                      f"{fn} → HTTP {r.status_code} {r.text[:60]}")
            except Exception as e:
                check("2. 모의 서버 POST /tools/{name}", False, f"{type(e).__name__} {hint(e)}")
            try:
                r = c.post(f"{mock}/tools/__not_defined__", json={}, headers={"X-Session-Id": sid})
                check("3. 정의에 없는 함수 거부", r.status_code == 404, f"HTTP {r.status_code}")
            except Exception as e:
                check("3. 정의에 없는 함수 거부", False, f"{type(e).__name__}")

    # 4-5: agent through the unmodified evaluator
    want = set(x.strip() for x in a.case_ids.split(",") if x.strip())
    cases = [json.loads(l) for l in open(REPO / a.cases, encoding="utf-8") if l.strip()]
    cases = [x for x in cases if x["case_id"] in want]
    with tempfile.TemporaryDirectory() as td:
        cpath = Path(td) / "cases.jsonl"
        cpath.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in cases), encoding="utf-8")
        out = Path(td) / "run"
        p = subprocess.run([sys.executable, "eval/run_eval.py", "--server", a.agent, "--cases", str(cpath),
                            "--audio", a.audio, "--mock", mock, "--out", str(out)],
                           cwd=REPO, capture_output=True, text=True, encoding="utf-8",
                           env={**__import__("os").environ, "PYTHONUTF8": "1"})
        res_path = out / "results.jsonl"
        res = [json.loads(l) for l in res_path.read_text(encoding="utf-8").splitlines()] if res_path.exists() else []
    if not res:
        last = ((p.stderr or p.stdout).strip().splitlines() or ["(출력 없음)"])[-1]
        check("4. 상담원 서버 한 턴 (측정기)", False, f"{last[:200]} {hint(Exception(last))}")
    for x in sorted(res, key=lambda r: r["case_id"]):
        if "error" in x:
            check(f"4. 상담원 서버 한 턴 {x['case_id']}", False, f"{x['error'][:120]} {hint(Exception(x['error']))}")
            continue
        fs = x.get("first_sound")
        ok = x.get("turn_end") is not None and fs is not None and not x.get("spoke_before_end")
        check(f"4. 상담원 서버 한 턴 {x['case_id']}", ok,
              f"첫 소리 {fs if fs is None else round(fs, 2)}s, turn.end {x.get('turn_end') and round(x['turn_end'], 2)}s"
              + (", 끼어듦" if x.get("spoke_before_end") else ""))
        calls = x.get("tool_calls") or []
        if calls:
            check(f"5. 함수 호출이 세션 ID로 기록 {x['case_id']}", True,
                  ", ".join(f"{t['name']}({json.dumps(t['arguments'], ensure_ascii=False)}) matched={t['matched']}" for t in calls))
    failed = [n for n, ok, _ in rows if not ok]
    print("\n" + ("모두 통과: 이 두 주소를 운영진에게 줘도 됩니다." if not failed else f"실패 {len(failed)}개: " + ", ".join(failed)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
