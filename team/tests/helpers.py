"""Test client that follows the starter-kit evaluator protocol (eval/run_eval.py):
session.start JSON -> 640-byte PCM frames (speech, then endless silence) -> collect agent
messages until turn.end. `speed` > 1 sends faster than real time (the agent's endpointing
counts frames, so logic is unchanged; only wall-clock latencies are not meaningful)."""
from __future__ import annotations

import asyncio
import json
import time

import numpy as np
import websockets

RATE = 16000
FRAME = 640


def speechlike(sec: float, amp: int = 6000, seed: int = 0) -> bytes:
    """Amplitude-modulated noise+tone; passes the energy VAD (tests use vad=energy)."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(RATE * sec)) / RATE
    env = 0.6 + 0.4 * np.sin(2 * np.pi * 4 * t)
    x = (np.sin(2 * np.pi * 220 * t) * 0.6 + rng.normal(0, 0.3, len(t))) * env * amp
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


def silence(sec: float) -> bytes:
    return b"\0" * (int(RATE * sec) * 2)


async def run_client(url, session_id, pcm, tools_url, case_id="T", caller=None, history=None,
                     speed=1.0, tail_timeout=25.0, first_message=None):
    out = {"session_id": session_id, "audio_bytes": 0, "events": [], "msgs": [],
           "first_agent_frame_idx": None, "frames_sent": 0, "after_turn_end": 0}
    speech_frames = len(pcm) // FRAME
    async with websockets.connect(url, max_size=None) as ws:
        if first_message is not None:
            await ws.send(first_message)
        else:
            await ws.send(json.dumps({"type": "session.start", "session_id": session_id, "case_id": case_id,
                                      "now": "2026-11-21 11:00", "history": history or [], "caller": caller,
                                      "tools_url": tools_url,
                                      "audio": {"encoding": "pcm_s16le", "sample_rate": RATE, "channels": 1}},
                                     ensure_ascii=False))
        done = asyncio.Event()
        t0 = time.perf_counter()

        async def sender():
            k = 0
            while not done.is_set():
                i = k * FRAME
                c = pcm[i:i + FRAME] if i < len(pcm) else b"\0" * FRAME
                c = c + b"\0" * (FRAME - len(c))
                try:
                    await ws.send(c)
                except Exception:
                    return
                k += 1
                out["frames_sent"] = k
                w = t0 + k * 0.02 / speed - time.perf_counter()
                if w > 0:
                    await asyncio.sleep(w)
                if (k - speech_frames) * 0.02 > tail_timeout:
                    out["timeout"] = True
                    done.set()

        async def receiver():
            try:
                async for m in ws:
                    turn_ended = any(e.get("type") == "turn.end" for e in out["events"])
                    if isinstance(m, bytes):
                        if turn_ended:
                            out["after_turn_end"] += 1
                        out["audio_bytes"] += len(m)
                        out["msgs"].append(("audio", len(m)))
                        if out["first_agent_frame_idx"] is None:
                            out["first_agent_frame_idx"] = out["frames_sent"]
                    else:
                        e = json.loads(m)
                        out["events"].append(e)
                        out["msgs"].append(("json", e.get("type")))
                        if out["first_agent_frame_idx"] is None:
                            out["first_agent_frame_idx"] = out["frames_sent"]
                        if e.get("type") == "turn.end":
                            await asyncio.sleep(0.2)  # catch anything sent after turn.end
                            done.set()
                            return
            except websockets.ConnectionClosed as e:
                out["closed"] = (e.rcvd.code if e.rcvd else None)
                done.set()

        st, rt = asyncio.create_task(sender()), asyncio.create_task(receiver())
        await done.wait()
        st.cancel(); rt.cancel()
    out["speech_frames"] = speech_frames
    out["text"] = " ".join(e.get("text", "") for e in out["events"] if e.get("type") == "agent.transcript")
    out["turn_end"] = any(e.get("type") == "turn.end" for e in out["events"])
    return out
