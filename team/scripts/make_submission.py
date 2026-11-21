"""Build the submission zip (official README 7장) and run the pre-submission check.

    python -m team.scripts.make_submission --official <공식 자료 폴더> [--out submission.zip] [--check] [--real]

Zip contents
  * every COMMITTED file of HEAD (git archive) - uncommitted changes are NOT included (warned)
  * official/tools.json, official/mock_db.json  from the official kit (not in the repository:
    contest data must not leave the event; it goes only into the zip handed to the organisers)
  * .env (API keys) if present - warned if missing
  top level: start_server.sh, start_mock.sh, requirements.txt (README 7장)

--check (README 7장 「제출 전 확인」): unzip into a new folder, new venv with --python (default
python3.11 = contest container), pip install -r requirements.txt, bash start_mock.sh <kit mock_db>,
bash start_server.sh, official eval/run_eval.py --limit N, official eval/score.py --no-judge.
Without --real the agent runs with test doubles (fake STT by case_id, scripted LLM, tone TTS)
via environment variables that win over .env: this checks install / start / protocol / relay,
not answer quality. --real uses .env as is (spends credits).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NEED_KEYS = ("OPENAI_API_KEY", "SONIOX_API_KEY", "GEMINI_API_KEY")
# non-secret settings the contest requires (official README: 행사 지원 모델). Without them the
# agent falls back to the code defaults (older OpenAI STT/TTS), which are not the contest models.
OFFICIAL_SETTINGS = {
    "AGENT_STT_PROVIDER": "soniox",
    "AGENT_TTS_PROVIDER": "gemini",
    "AGENT_LLM_MODEL": "gpt-5.4-mini",
    "AGENT_SONIOX_MODEL": "stt-rt-v6",
    "AGENT_GEMINI_TTS_MODEL": "gemini-3.8-flash-lite-tts",
}


def env_warnings(env_path: Path) -> list[str]:
    """Warnings about .env. Key VALUES are never read into the output; only key names, and the
    values of the non-secret settings above, are compared."""
    pairs = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            pairs[k.strip()] = v.strip().strip('"').strip("'")
    out = [f".env 에 키 이름이 없음: {k}" for k in NEED_KEYS if not pairs.get(k)]
    for k, want in OFFICIAL_SETTINGS.items():
        if k not in pairs:
            out.append(f".env 에 {k}={want} 줄이 없음 -> 코드 기본값(공식 모델 아님)으로 동작")
        elif pairs[k] != want:
            out.append(f".env 의 {k}={pairs[k]} 가 공식 값 {want} 과 다름")
    if pairs.get("AGENT_LOG_TEXT", "0").lower() in ("1", "true", "yes", "on"):
        out.append(".env 에 AGENT_LOG_TEXT=1 -> 손님 말·답 텍스트가 기록 파일에 남음. 제출 때는 빼기")
    return out


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def build(a) -> Path:
    kit = Path(a.official)
    tools, db = kit / "data" / "brand" / "tools.json", kit / "data" / "mock_db.json"
    for p in (tools, db):
        if not p.exists():
            sys.exit(f"공식 자료에 {p} 가 없습니다 (--official 은 압축을 푼 공식 자료 폴더)")
    dirty = sh(["git", "status", "--porcelain"], cwd=REPO).stdout.strip()
    if dirty:
        print("주의: 커밋하지 않은 변경은 압축에 들어가지 않습니다:\n" + dirty, file=sys.stderr)
    out = Path(a.out).resolve()
    sh(["git", "archive", "--format=zip", "-o", str(out), "HEAD"], cwd=REPO)
    env = REPO / ".env" if not a.env else Path(a.env)
    with zipfile.ZipFile(out, "a", zipfile.ZIP_DEFLATED) as z:
        z.write(tools, "official/tools.json")
        z.write(db, "official/mock_db.json")
        if env.exists():
            z.write(env, ".env")
            for w in env_warnings(env):
                print("주의: " + w, file=sys.stderr)
        else:
            print(f"주의: {env} 가 없어 키 없이 압축했습니다 (제출 때는 .env 필요)", file=sys.stderr)
        names = z.namelist()
    top = {"start_server.sh", "start_mock.sh", "requirements.txt"}
    if not top <= set(names):
        sys.exit("압축 맨 위에 없는 파일: " + ", ".join(sorted(top - set(names))))
    bad = [n for n in names if n.startswith((".venv/", "venv/")) or "/__pycache__/" in n]
    print(f"압축: {out} ({out.stat().st_size / 1e6:.1f} MB, 파일 {len(names)}개)"
          + (f", 넣으면 안 되는 파일 {len(bad)}개" if bad else ""))
    return out


def wait_port(port, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


def check(a, zpath: Path):
    kit = Path(a.official).resolve()
    work = Path(tempfile.mkdtemp(prefix="submission_check_"))
    app = work / "app"
    with zipfile.ZipFile(zpath) as z:
        z.extractall(app)
    print(f"[1] 새 폴더에 풀기: {app}")
    venv = work / "venv"
    sh([a.python, "-m", "venv", str(venv)])
    py = venv / "bin" / "python"
    ver = sh([str(py), "-c", "import sys;print(sys.version.split()[0])"]).stdout.strip()
    r = subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=app,
                       capture_output=True, text=True)
    print(f"[2] 파이썬 {ver} 새 가상환경에 requirements.txt 설치: {'성공' if r.returncode == 0 else '실패'}")
    if r.returncode:
        print(r.stderr[-1500:])
        return False
    env = {**os.environ, "PATH": f"{venv / 'bin'}{os.pathsep}{os.environ['PATH']}", "PYTHONUNBUFFERED": "1"}
    if not a.real:
        cases = {}
        for line in (kit / "data" / "practice_cases.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                c = json.loads(line)
                cases[c["case_id"]] = c.get("customer_utterance", "")
        fake = work / "fake_stt.json"  # outside the app folder
        fake.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
        env.update(AGENT_STT_PROVIDER="fake", AGENT_STT_FAKE_TEXT=str(fake), AGENT_LLM_PROVIDER="scripted",
                   AGENT_TTS_PROVIDER="tone", AGENT_EOU_MODE="vad")
    logs = {n: open(work / f"{n}.log", "w") for n in ("mock", "server")}
    procs = [subprocess.Popen(["bash", "start_mock.sh", str(kit / "data" / "mock_db.json")], cwd=app, env=env,
                              stdout=logs["mock"], stderr=subprocess.STDOUT),
             subprocess.Popen(["bash", "start_server.sh"], cwd=app, env=env, stdout=logs["server"], stderr=subprocess.STDOUT)]
    ok = True
    try:
        up = wait_port(8100) and wait_port(9000)
        print(f"[3] bash start_mock.sh / bash start_server.sh: {'둘 다 시작됨 (8100, 9000)' if up else '시작 실패'}")
        if not up:
            for n in logs:
                print(f"--- {n}.log\n" + (work / f"{n}.log").read_text(errors="ignore")[-1500:])
            return False
        run = work / "run"
        r = subprocess.run([str(py), "run_eval.py", "--server", "ws://127.0.0.1:9000", "--mock", "http://127.0.0.1:8100",
                            "--cases", "../data/practice_cases.jsonl", "--audio", "../data/audio/practice",
                            "--out", str(run), "--limit", str(a.limit)], cwd=kit / "eval", env=env,
                           capture_output=True, text=True)
        res = [json.loads(l) for l in (run / "results.jsonl").read_text(encoding="utf-8").splitlines()] \
            if (run / "results.jsonl").exists() else []
        errs = [x for x in res if x.get("error")]
        no_end = [x for x in res if not x.get("error") and x.get("turn_end") is None]
        print(f"[4] 공식 측정기 --limit {a.limit}: 문항 {len(res)}개, 오류 {len(errs)}, turn.end 없음 {len(no_end)}")
        for x in res:
            calls = ", ".join(f"{c['name']}({'기록 일치' if c.get('matched') else '기록 없음'})" for c in x.get("tool_calls", []))
            print(f"    {x['case_id']}: 첫 소리 {x.get('first_sound')}  함수 [{calls}]" + (f"  오류 {x['error'][:80]}" if x.get("error") else ""))
        silent = [x for x in res if not x.get("error") and x.get("first_sound") is None]
        ok = bool(res) and not errs and not no_end and not silent and len(res) == a.limit
        # quiet failures: the server stays up and sends turn.end, but STT/TTS/LLM fail every turn
        slog = (work / "server.log").read_text(errors="ignore")
        warm_fail = slog.count("pre-synthesis of")
        turn_err = len(re.findall(r"outcome=error", slog))
        if silent:
            print(f"    주의: 소리를 하나도 안 낸 문항 {len(silent)}개 (속도·응대 0점)")
        if warm_fail or turn_err:
            print(f"    주의: 시작 때 고정 문장 합성 실패 {warm_fail}개, 턴 처리 오류 {turn_err}개 -> 키·모델 이름·인증 방식 확인"
                  " (Soniox 인증 오류면 .env 에 AGENT_SONIOX_AUTH=header)")
            ok = False
        if not res:
            print(r.stdout[-800:], r.stderr[-800:])
        s = subprocess.run([str(py), "score.py", "--run", str(run), "--cases", "../data/practice_cases.jsonl"]
                           + ([] if a.real else ["--no-judge"]), cwd=kit / "eval", env=env, capture_output=True, text=True)
        print("[5] 공식 채점기" + ("" if a.real else " (--no-judge: 함수·속도만)") + ":\n" +
              "\n".join("    " + l for l in (s.stdout or s.stderr).strip().splitlines()[-12:]))
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in logs.values():
            f.close()
    print(("통과: " if ok else "실패: ") + f"점검 폴더 {work} (로그 mock.log, server.log)")
    if ok and not a.keep:
        shutil.rmtree(work, ignore_errors=True)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--official", required=True, help="압축을 푼 공식 자료 폴더 (directon-2026-voice-cs)")
    ap.add_argument("--out", default="submission.zip")
    ap.add_argument("--env", default=None, help="넣을 .env (기본: 저장소 루트의 .env)")
    ap.add_argument("--check", action="store_true", help="README 7장 제출 전 확인까지 실행")
    ap.add_argument("--real", action="store_true", help="점검 때 .env 의 실제 모델 사용 (크레딧 사용)")
    ap.add_argument("--python", default=shutil.which("python3.11") or sys.executable,
                    help="점검용 가상환경 파이썬 (기본 python3.11 = 운영진 컨테이너)")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--keep", action="store_true", help="통과해도 점검 폴더를 지우지 않음")
    a = ap.parse_args()
    z = build(a)
    if a.check:
        sys.exit(0 if check(a, z) else 1)


if __name__ == "__main__":
    main()
