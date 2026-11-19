"""말 끝 판단 비교 실험용 음성 서버 (v1) — 듣기 · 말 끝 판단 · STT · TTS

최종 지정 모델 (11/19 운영진 공지)
  STT: Soniox (stt-rt-v6)   TTS: Gemini 2.5 Flash TTS   LLM: gpt-5.4 mini (주연님 파트)

흐름
  측정기 음성(16kHz) ─→ Soniox 실시간 인식 (16kHz 그대로, 변환 없음)
     Soniox가 말 끝이라고 판단하면 <end> 토큰을 보냄 (모델 기반 말 끝 감지)
  <end>가 오면 바로 답 만들기 시작 (LLM 자리 = make_reply, 지금은 따라 말하기)
     → 답의 첫 문장을 먼저 TTS로 만들고 나머지는 동시에 만듦 (첫 소리를 빠르게)
     → Gemini 음성 24kHz를 16kHz로 바꿔서 보냄 → turn.end
  고객이 다시 말하기 시작하면 (Soniox 새 토큰) 만들던 답은 버리고 다시 기다림
  안전장치: 우리 쪽에서 잰 침묵이 MIN_SILENT_MS보다 짧으면 말하지 않음 (끼어들기 방지)

실행
  export SONIOX_API_KEY=...
  export GEMINI_API_KEY=...
  python team/scripts/endpoint_server.py --port 9000

  python team/scripts/endpoint_server.py --port 9000 --dry   # 키 없이: 말 끝 = 소리 크기, 답 = 삐 소리

조절할 값 (환경변수, 실험 3 대상)
  MAX_ENDPOINT_DELAY_MS   Soniox 말 끝 최대 대기 (500~3000, 기본 1500)
  ENDPOINT_LATENCY        Soniox endpoint_latency_adjustment_level (0~3, 기본 2)
  ENDPOINT_SENSITIVITY    Soniox endpoint_sensitivity (-1.0~1.0, 기본 0.3)
  MIN_SILENT_MS           말하기 전 최소 침묵 (기본 300)
  DRY_END_MS              --dry 에서 말 끝으로 볼 침묵 (기본 1300)
"""
import argparse, asyncio, base64, json, os, re, time
import numpy as np, httpx, websockets
from scipy.signal import resample_poly

RATE = 16000
SPEECH_RMS = 500
MAX_ENDPOINT_DELAY_MS = int(os.getenv("MAX_ENDPOINT_DELAY_MS", "1500"))
ENDPOINT_LATENCY = int(os.getenv("ENDPOINT_LATENCY", "2"))
ENDPOINT_SENSITIVITY = float(os.getenv("ENDPOINT_SENSITIVITY", "0.3"))
MIN_SILENT_MS = int(os.getenv("MIN_SILENT_MS", "300"))
DRY_END_MS = int(os.getenv("DRY_END_MS", "1300"))

SONIOX_KEY = os.getenv("SONIOX_API_KEY", "")
SONIOX_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
SONIOX_MODEL = os.getenv("SONIOX_MODEL", "stt-rt-v6")

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_TTS_MODEL = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Kore")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TTS_STYLE = "차분하고 친절한 한국어 전화 상담원 말투로 또박또박 말하세요: "


def rms(pcm):
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if len(a) else 0.0


def down_24k_to_16k(pcm):
    pcm = pcm[: len(pcm) - len(pcm) % 2]
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return np.clip(resample_poly(a, 2, 3), -32768, 32767).astype(np.int16).tobytes()


def beep(sec=0.4, hz=440):
    t = np.arange(int(RATE * sec)) / RATE
    return (np.sin(2 * np.pi * hz * t) * 8000).astype(np.int16).tobytes()


def split_first_sentence(text):
    """첫 문장과 나머지로 나눈다. 첫 문장을 먼저 TTS로 만들어 첫 소리를 빠르게."""
    m = re.search(r"[.?!요다]\s", text)
    if not m:
        return text, ""
    cut = m.end()
    return text[:cut].strip(), text[cut:].strip()


async def make_reply(text, start):
    """LLM 자리 (주연님 파트, gpt-5.4 mini). 지금은 들은 말을 그대로 따라 말한다."""
    return f"네, {text} 라고 말씀하셨어요."


async def gemini_tts(http, text):
    """Gemini TTS로 음성을 만들어 16kHz PCM으로 돌려준다."""
    body = {
        "contents": [{"parts": [{"text": TTS_STYLE + text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": GEMINI_VOICE}}},
        },
    }
    r = await http.post(GEMINI_URL.format(model=GEMINI_TTS_MODEL), json=body, timeout=30,
                        headers={"x-goog-api-key": GEMINI_KEY})
    r.raise_for_status()
    data = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    pcm = base64.b64decode(data)
    if pcm[:4] == b"RIFF":            # WAV로 오면 헤더(44바이트) 떼기
        pcm = pcm[44:]
    return down_24k_to_16k(pcm)


class Turn:
    """한 문항(연결 하나)의 상태"""

    def __init__(self, ws, http, start, dry):
        self.ws, self.http, self.start, self.dry = ws, http, start, dry
        self.silent_ms = 0            # 고객이 연속으로 조용했던 시간 (우리가 직접 잼)
        self.heard = False
        self.final_text = ""          # Soniox가 확정한 글자
        self.end_ok = False           # 말 끝 신호(<end>)를 받았나
        self.chunks = []              # 미리 만들어둔 답 음성 (순서대로)
        self.gen_done = False
        self.gen = None
        self.reply = None
        self.speaking = False
        self.finished = asyncio.Event()
        self.t = {}

    def mark(self, k):
        self.t.setdefault(k, time.perf_counter())

    # ── 말 끝 신호가 오면 답 만들기 시작 ──
    def on_end(self):
        if self.speaking:
            return
        self.end_ok = True
        self.mark("end_signal")
        if self.gen:
            self.gen.cancel()
        self.chunks, self.gen_done = [], False
        self.gen = asyncio.create_task(self._generate(self.final_text.strip()))

    # ── 고객이 다시 말하면 만들던 답 버리기 ──
    def on_resume(self):
        if self.speaking or not self.end_ok:
            return
        self.end_ok = False
        if self.gen:
            self.gen.cancel()
            self.gen = None
        self.chunks, self.gen_done = [], False
        self.t.pop("end_signal", None)

    async def _generate(self, text):
        try:
            if self.dry:
                self.chunks.append(beep())
            else:
                self.reply = await make_reply(text, self.start)
                self.mark("llm_done")
                first, rest = split_first_sentence(self.reply)
                t1 = asyncio.create_task(gemini_tts(self.http, first))
                t2 = asyncio.create_task(gemini_tts(self.http, rest)) if rest else None
                self.chunks.append(await t1)
                self.mark("tts_first")
                if t2:
                    self.chunks.append(await t2)
            self.gen_done = True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(self.start.get("case_id"), "답 만들기 실패:", e)
            self.chunks.append(beep(hz=220))
            self.gen_done = True

    # ── 말하기: 말 끝 신호 + 답 준비 + 최소 침묵이면 보낸다 ──
    async def speaker(self):
        while not self.finished.is_set():
            if self.end_ok and self.chunks and self.silent_ms >= MIN_SILENT_MS:
                self.speaking = True
                break
            await asyncio.sleep(0.01)
        self.mark("first_send")
        if self.reply:
            await self.ws.send(json.dumps({"type": "agent.transcript", "text": self.reply}, ensure_ascii=False))
        sent = 0
        while True:
            while sent < len(self.chunks):
                await self.ws.send(self.chunks[sent])
                sent += 1
            if self.gen_done:
                break
            await asyncio.sleep(0.01)
        await self.ws.send(json.dumps({"type": "turn.end"}))
        self.finished.set()


async def soniox_reader(stt, turn):
    """Soniox에서 오는 토큰 처리. <end> = 말 끝, 그 뒤 새 토큰 = 고객이 다시 말함"""
    async for raw in stt:
        msg = json.loads(raw)
        if msg.get("error_code") or msg.get("error_type"):
            print("Soniox 오류:", msg)
            continue
        for tok in msg.get("tokens", []):
            text = tok.get("text", "")
            if text == "<end>":
                print(turn.start.get("case_id"), "인식:", turn.final_text.strip())
                turn.on_end()
            else:
                if turn.end_ok and text.strip():
                    turn.on_resume()
                if tok.get("is_final"):
                    turn.final_text += text
        if msg.get("finished"):
            break


async def handler(ws, http, dry):
    start = json.loads(await ws.recv())
    assert start["type"] == "session.start"
    turn = Turn(ws, http, start, dry)
    spk = asyncio.create_task(turn.speaker())

    stt = reader = None
    if not dry:
        stt = await websockets.connect(SONIOX_URL, additional_headers={"Authorization": f"Bearer {SONIOX_KEY}"},
                                       max_size=None)
        await stt.send(json.dumps({
            "model": SONIOX_MODEL,
            "audio_format": "pcm_s16le", "sample_rate": RATE, "num_channels": 1,
            "language_hints": ["ko"],
            "enable_endpoint_detection": True,
            "max_endpoint_delay_ms": MAX_ENDPOINT_DELAY_MS,
            "endpoint_latency_adjustment_level": ENDPOINT_LATENCY,
            "endpoint_sensitivity": ENDPOINT_SENSITIVITY,
        }))
        reader = asyncio.create_task(soniox_reader(stt, turn))

    async def receive():
        async for m in ws:
            if not isinstance(m, bytes):
                continue
            if rms(m) >= SPEECH_RMS:
                turn.heard, turn.silent_ms = True, 0
                turn.t.pop("local_end", None)
                if dry:
                    turn.on_resume()
            elif turn.heard:
                turn.silent_ms += len(m) * 1000 // (RATE * 2)
                turn.mark("local_end")
                if dry and turn.silent_ms >= DRY_END_MS and not turn.end_ok:
                    turn.on_end()
            if stt and not turn.speaking:
                await stt.send(m)

    recv = asyncio.create_task(receive())
    try:
        await asyncio.wait_for(turn.finished.wait(), timeout=22)
    except asyncio.TimeoutError:
        print(start.get("case_id"), "시간 초과 → turn.end")
        await ws.send(json.dumps({"type": "turn.end"}))
    finally:
        for task in (recv, reader, spk, turn.gen):
            if task:
                task.cancel()
        if stt:
            try:
                await stt.send("")       # 빈 텍스트 프레임 = Soniox 스트림 끝
                await stt.close()
            except Exception:
                pass

    t0 = turn.t.get("local_end")
    if t0:
        log = {k: round(v - t0, 2) for k, v in turn.t.items() if k != "local_end"}
        print(start.get("case_id"), "말 끝 기준 시간(초):", log)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--dry", action="store_true", help="키 없이 테스트 (말 끝=소리 크기, 답=삐)")
    a = ap.parse_args()
    if not a.dry and not (SONIOX_KEY and GEMINI_KEY):
        raise SystemExit("SONIOX_API_KEY, GEMINI_API_KEY가 필요해요. 키 없이 시험하려면 --dry 를 붙이세요.")
    async with httpx.AsyncClient() as http:
        async with websockets.serve(lambda ws: handler(ws, http, a.dry), "0.0.0.0", a.port, max_size=None):
            if a.dry:
                print(f"voice server on :{a.port} | DRY(키 없음) | DRY_END_MS={DRY_END_MS} MIN_SILENT_MS={MIN_SILENT_MS}")
            else:
                print(f"voice server on :{a.port} | Soniox {SONIOX_MODEL} + Gemini {GEMINI_TTS_MODEL} | "
                      f"max_delay={MAX_ENDPOINT_DELAY_MS} latency={ENDPOINT_LATENCY} "
                      f"sensitivity={ENDPOINT_SENSITIVITY} min_silent={MIN_SILENT_MS}")
            await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
