"""DIRECT:ON 2026 사전 연습용 WebSocket 음성 측정기 (공식 채점용 아님).

실행 예:
  python eval/run_eval.py --server ws://127.0.0.1:9000 \
    --cases data/demo_cases.jsonl --audio data/audio/demo \
    --mock http://127.0.0.1:8100 --out runs/demo --limit 3

출력: runs/demo/results.jsonl, runs/demo/agent_audio/*.wav
이 측정기는 가상 음성 송수신과 첫 응답 시간만 시험합니다.
공식 시험·공식 점수·실제 상담 정답 데이터는 포함하지 않습니다.
"""
import argparse, asyncio, json, os, time, uuid, wave
import numpy as np
import httpx, websockets

RATE = 16000
FRAME = 320                 # 20ms
SILENCE_RMS = 300           # 이보다 작은 프레임은 무음으로 본다
TAIL_TIMEOUT = 25.0         # 말 끝 뒤 최대 대기(초)


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
                "now": case["now"], "history": case["history"], "caller": case.get("customer_profile"), "tools_url": args.mock,
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

    # 함수 호출 기록 (모의 서버) → 마지막 함수 응답 뒤 첫 소리
    async with httpx.AsyncClient(timeout=10) as c:
        calls = (await c.get(args.mock.rstrip("/") + "/calls", params={"session_id": sid})).json()
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
    ap.add_argument("--mock", default="http://localhost:8100")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    cases = [json.loads(l) for l in open(args.cases)]
    meta = json.load(open(os.path.join(args.audio, "meta.json")))
    cases = [c for c in cases if c["case_id"] in meta]
    path = os.path.join(args.out, "results.jsonl")
    done = {json.loads(l)["case_id"] for l in open(path)} if os.path.exists(path) else set()
    todo = [c for c in cases if c["case_id"] not in done][: args.limit or None]
    sem = asyncio.Semaphore(args.concurrency)

    async def one(c):
        async with sem:
            return await run_case(c, meta[c["case_id"]], args)

    with open(path, "a") as f:
        for fut in asyncio.as_completed([one(c) for c in todo]):
            r = await fut
            f.write(json.dumps(r, ensure_ascii=False) + "\n"); f.flush()
            fs = r.get("first_sound")
            if "error" in r:
                print(f"{r['case_id']}: ERROR {r['error']}", flush=True)
            else:
                timing = f"첫 소리 {fs:.2f}s" if fs is not None else "소리 없음"
                print(f"{r['case_id']}: {timing} | 함수 {len(r.get('tool_calls', []))}개", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
