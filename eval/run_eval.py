"""측정기 — 참가자 음성 상담 서버에 손님 음성을 실시간으로 흘려 넣고, 답 음성과 시간을 기록한다.

python3 run_eval.py --server ws://localhost:9000 --cases ../data/practice_cases.jsonl \
                    --audio ../data/audio/practice --mock http://localhost:8100 --out runs/try1 [--limit 20] [--concurrency 4]

결과: <out>/results.jsonl (문항별 시간·함수 호출), <out>/agent_audio/<case_id>.wav (AI 답 음성), <out>/run_info.json
규격은 ../SPEC.md 를 본다. 채점은 score.py.

함수 중계: 측정기는 --relay-port 에 작은 HTTP 중계를 띄우고, 상담 서버에는 session.start 의 tools_url 로
이 중계 주소를 준다. 중계는 받은 호출을 --mock(참가팀 모의 함수 서버)에 넘기고, 호출 기록과 응답 지연은
측정기가 직접 맡는다. 그래서 모의 함수 서버는 맞는 결과만 돌려주면 된다.
"""
import argparse, asyncio, hashlib, json, os, threading, time, urllib.request, uuid, wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import numpy as np
import websockets

RATE = 16000
FRAME = 320                 # 20ms
SILENCE_RMS = 300           # 이보다 작은 프레임은 무음으로 본다
TAIL_TIMEOUT = 25.0         # 말 끝 뒤 최대 대기(초)


# 함수별 최소 응답 시간(초) — 실제 쇼핑몰 시스템의 응답 시간. 모의 서버가 더 빨리 답해도 이 시간까지 기다렸다 넘긴다.
DELAY = {"get_order_info": 1.2, "get_return_info": 1.0, "search_products": 1.5, "cancel_order": 1.5,
         "return_order": 1.5, "return_order_item": 1.5, "exchange_order": 1.5, "request_callback": 0.8}


class Relay:
    """상담 서버 → (측정기 중계) → 모의 함수 서버. 호출 기록·지연은 여기서 처리한다."""

    def __init__(self, mock, port):
        self.mock, self.calls, self.lock = mock.rstrip("/"), {}, threading.Lock()
        relay = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                code, body = relay.forward("GET", self.path, None)
                self._send(code, body)

            def do_POST(self):
                t0 = time.time()
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"
                code, body = relay.forward("POST", self.path, raw)
                name = self.path.rstrip("/").rsplit("/", 1)[-1]
                time.sleep(max(0.0, DELAY.get(name, 1.0) - (time.time() - t0)))
                try:
                    args = json.loads(raw)
                except ValueError:
                    args = None
                try:
                    status = json.loads(body).get("status")
                except Exception:
                    status = None
                rec = {"name": name, "arguments": args, "http": code, "status": status, "matched": status == "ok",
                       "t_request": t0, "t_response": time.time()}
                with relay.lock:
                    relay.calls.setdefault(self.headers.get("X-Session-Id"), []).append(rec)
                self._send(code, body)

        self.server = ThreadingHTTPServer(("0.0.0.0", port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def forward(self, method, path, body):
        req = urllib.request.Request(self.mock + path, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            return 502, json.dumps({"status": "error", "message": f"모의 함수 서버 연결 실패: {e!r}"[:200]}).encode()

    def take(self, sid):
        with self.lock:
            return self.calls.pop(sid, [])


def load_wav(path):
    with wave.open(path) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2, path
        return w.readframes(w.getnframes())


def rms(b):
    a = np.frombuffer(b, dtype=np.int16).astype(np.float32)
    return float(np.sqrt((a * a).mean())) if len(a) else 0.0


async def run_case(case, meta, args):
    sid = f"{case['case_id']}-{uuid.uuid4().hex[:8]}"
    pcm = load_wav(os.path.join(args.audio, case["case_id"] + ".wav"))
    speech_end_off = meta["speech_end"]          # 음성 파일 안에서 말이 끝나는 시각(초)
    out = {"case_id": case["case_id"], "session_id": sid}
    agent_frames = []                            # (도착시각, bytes)
    events = []
    try:
        async with websockets.connect(args.server, max_size=None, open_timeout=10) as ws:
            await ws.send(json.dumps({
                "type": "session.start", "session_id": sid, "case_id": case["case_id"],
                "now": case["now"], "history": case["history"], "caller": case.get("customer_profile"), "tools_url": args.relay_url,
                "audio": {"encoding": "pcm_s16le", "sample_rate": RATE, "channels": 1}}, ensure_ascii=False))
            t0 = time.perf_counter()
            speech_end = t0 + speech_end_off
            done = asyncio.Event()

            async def sender():
                silence = b"\0" * (FRAME * 2)
                k = 0
                while not done.is_set():
                    i = k * FRAME * 2
                    chunk = pcm[i:i + FRAME * 2] if i < len(pcm) else silence   # 말이 끝나도 전화선처럼 무음을 계속 보낸다
                    if len(chunk) < FRAME * 2:
                        chunk += b"\0" * (FRAME * 2 - len(chunk))
                    await ws.send(chunk)
                    k += 1
                    wait = t0 + k * 0.02 - time.perf_counter()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    if time.perf_counter() > speech_end + TAIL_TIMEOUT:
                        done.set()

            async def receiver():
                async for m in ws:
                    now = time.perf_counter()
                    if isinstance(m, bytes):
                        agent_frames.append((now, m))
                    else:
                        e = json.loads(m)
                        events.append({"t": now - speech_end, **e})
                        if e.get("type") == "turn.end":
                            done.set()
                            return

            st = asyncio.create_task(sender())
            rt = asyncio.create_task(receiver())
            await done.wait()
            st.cancel(); rt.cancel()
    except Exception as e:
        out["error"] = repr(e)[:300]
        return out

    # 시간 계산: 말 끝 → 첫 소리
    voiced = [t for t, b in agent_frames if rms(b) >= SILENCE_RMS]
    out["first_sound"] = (voiced[0] - speech_end) if voiced else None
    out["spoke_before_end"] = bool(voiced and voiced[0] < speech_end)   # 손님 말 도중에 끼어듦
    out["turn_end"] = next((e["t"] for e in events if e.get("type") == "turn.end"), None)
    out["agent_text"] = " ".join(e.get("text", "") for e in events if e.get("type") == "agent.transcript")

    # 함수 호출 기록 (측정기 중계가 직접 남긴 것) → 마지막 함수 응답 뒤 첫 소리
    await asyncio.sleep(0.2)
    calls = args.relay.take(sid)
    wall0 = time.time() - (time.perf_counter() - speech_end)            # perf → 벽시계 환산
    out["tool_calls"] = [{"name": x["name"], "arguments": x["arguments"], "matched": x["matched"],
                          "t_response": x["t_response"] - wall0} for x in calls]
    if calls:
        last = max(x["t_response"] for x in calls) - wall0
        after = [t - speech_end for t in voiced if t - speech_end > last]
        out["after_tool_sound"] = (after[0] - last) if after else None

    # 답 음성 저장
    os.makedirs(os.path.join(args.out, "agent_audio"), exist_ok=True)
    with wave.open(os.path.join(args.out, "agent_audio", case["case_id"] + ".wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(b"".join(b for _, b in agent_frames))
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--cases", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--mock", default="http://localhost:8100", help="참가팀 모의 함수 서버 주소")
    ap.add_argument("--relay-port", type=int, default=8200, help="측정기 함수 중계가 받을 포트")
    ap.add_argument("--relay-url", default=None, help="상담 서버에 알려 줄 중계 주소 (기본 http://localhost:<relay-port>)")
    ap.add_argument("--sample", type=int, default=0, help="문항 N개만 고정된 순서로 뽑아 잰다 (0=전부)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()
    args.relay_url = args.relay_url or f"http://localhost:{args.relay_port}"
    args.relay = Relay(args.mock, args.relay_port)
    os.makedirs(args.out, exist_ok=True)
    cases = [json.loads(l) for l in open(args.cases, encoding="utf-8")]
    meta = json.load(open(os.path.join(args.audio, "meta.json"), encoding="utf-8"))
    cases = [c for c in cases if c["case_id"] in meta]
    if args.sample:
        cases = sorted(cases, key=lambda c: hashlib.md5(c["case_id"].encode()).hexdigest())[: args.sample]
    me = open(__file__, "rb").read()
    json.dump({"evaluator_sha256": hashlib.sha256(me).hexdigest(), "server": args.server, "mock": args.mock,
               "relay_url": args.relay_url, "cases_file": os.path.basename(args.cases), "cases": len(cases),
               "started": time.strftime("%Y-%m-%d %H:%M:%S")},
              open(os.path.join(args.out, "run_info.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    path = os.path.join(args.out, "results.jsonl")
    done = {json.loads(l)["case_id"] for l in open(path, encoding="utf-8")} if os.path.exists(path) else set()
    todo = [c for c in cases if c["case_id"] not in done][: args.limit or None]
    sem = asyncio.Semaphore(args.concurrency)

    async def one(c):
        async with sem:
            return await run_case(c, meta[c["case_id"]], args)

    with open(path, "a", encoding="utf-8") as f:
        for fut in asyncio.as_completed([one(c) for c in todo]):
            r = await fut
            f.write(json.dumps(r, ensure_ascii=False) + "\n"); f.flush()
            fs = r.get("first_sound")
            print(r["case_id"], r.get("error") or f"첫 소리 {fs:.2f}s" if fs is not None else "소리 없음",
                  f"함수 {len(r.get('tool_calls', []))}개" if "error" not in r else "", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
