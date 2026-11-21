"""규격 확인용 뼈대 서버 — AI 없이 주고받는 순서만 보여 준다.

하는 일:
  1. session.start 를 받는다
  2. 손님 음성을 받으며 음량으로 말 끝을 어림한다 (아주 거친 방식이다)
  3. 말이 끝났다고 보면 신호음을 답 음성 대신 보낸다
  4. turn.end 를 보낸다

받아쓰기, 응대 판단, 음성합성은 들어 있지 않다. 이 자리를 여러분의 설계로 채운다.
--demo-tool 을 주면 모의 함수 서버를 부르는 법도 보여 준다 (발신자 번호로 주문 조회).
이 서버로 측정기를 돌리면 속도 점수는 나오지만 응대 점수는 거의 0이다.

실행: python3 server.py --port 9000 [--demo-tool]
"""
import argparse, asyncio, json
import numpy as np, httpx, websockets

RATE = 16000
FRAME = 640                   # 20ms
SPEECH_RMS = 500              # 이보다 크면 말소리로 본다
END_SILENCE_MS = 800          # 말소리 뒤 이만큼 조용하면 말이 끝났다고 본다


def rms(pcm):
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if len(a) else 0.0


def beep(sec=0.4, hz=440):
    t = np.arange(int(RATE * sec)) / RATE
    return (np.sin(2 * np.pi * hz * t) * 8000).astype(np.int16).tobytes()


async def call_tool(http, start, name, args):
    """모의 함수 서버 호출. X-Session-Id 를 빠뜨리면 채점에서 호출하지 않은 것으로 본다."""
    r = await http.post(f"{start['tools_url'].rstrip('/')}/tools/{name}", json=args,
                        headers={"X-Session-Id": start["session_id"]}, timeout=30)
    return r.json()


async def handler(ws, http, demo_tool):
    start = json.loads(await ws.recv())
    assert start["type"] == "session.start"

    heard_speech, silent_ms = False, 0
    async for m in ws:                                 # 손님 음성 20ms 조각
        if not isinstance(m, bytes):
            continue
        if rms(m) >= SPEECH_RMS:
            heard_speech, silent_ms = True, 0
        elif heard_speech:
            silent_ms += len(m) * 1000 // (RATE * 2)
            if silent_ms >= END_SILENCE_MS:
                break

    async def drain():                                 # 측정기는 끝까지 무음을 보낸다 — 읽어서 버린다
        async for _ in ws:
            pass
    d = asyncio.create_task(drain())

    # ── 여기부터가 여러분이 채울 자리: 무엇을 들었는지, 어떻게 답할지 ──
    await ws.send(json.dumps({"type": "agent.transcript", "text": "(신호음)"}, ensure_ascii=False))
    await ws.send(beep())

    if demo_tool and start.get("caller"):
        result = await call_tool(http, start, "get_order_info", {"order_id": None, "buyer_cellphone": start["caller"]["phone"]})
        print(start["case_id"], "get_order_info →", json.dumps(result, ensure_ascii=False)[:120])
        await ws.send(beep(hz=660))                    # 조회 뒤 답 자리

    await ws.send(json.dumps({"type": "turn.end"}))
    d.cancel()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--demo-tool", action="store_true")
    a = ap.parse_args()
    async with httpx.AsyncClient() as http:
        async with websockets.serve(lambda ws: handler(ws, http, a.demo_tool), "0.0.0.0", a.port, max_size=None):
            print(f"skeleton server on :{a.port}")
            await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
