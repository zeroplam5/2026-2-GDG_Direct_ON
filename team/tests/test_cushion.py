"""Cushion phrase: when nothing has been said cushion_after_s after the end of the customer's speech,
the agent says "오래 기다려 주셔서 감사합니다." once, then the answer. Test doubles only (fake STT,
ScriptedLLM with a delay, ToneTTS) - no API keys."""
from team.agent.config import Config

from .helpers import run_client, silence, speechlike
from .test_agent_protocol import _metrics, agent, run, two_mocks  # noqa: F401  (fixtures)

CUSHION = Config().cushion_text


def _go(start, sid, tools_url):
    async def go():
        h = await start()
        return await run_client(h["url"], sid, speechlike(1.5) + silence(0.3), tools_url, case_id="ORDER_OK", speed=1)
    return run(go())


def _said(r):
    return [e["text"] for e in r["events"] if e.get("type") == "agent.transcript"]


def test_defaults_and_env(monkeypatch):
    cfg = Config()
    assert cfg.cushion is True and cfg.cushion_after_s == 5.0 and CUSHION == "오래 기다려 주셔서 감사합니다."
    monkeypatch.setenv("AGENT_CUSHION", "0")
    monkeypatch.setenv("AGENT_CUSHION_AFTER_S", "3.5")
    env = Config.from_env()
    assert env.cushion is False and env.cushion_after_s == 3.5


def test_slow_turn_gets_the_cushion_once_before_the_answer(agent, two_mocks):
    start, cfg = agent
    cfg.cushion_after_s = 1.0
    cfg.scripted_delay_ms = 2500          # the answer is not ready 1 s after the end of speech
    cfg.send_transcript_event = True
    (store, murl), _ = two_mocks
    r = _go(start, "SID-CUSH-SLOW", murl)
    said = _said(r)
    assert r["turn_end"] and said[0] == CUSHION and said.count(CUSHION) == 1
    assert any("배송중" in s for s in said[1:])           # the answer still comes, after the cushion
    assert [e["type"] for e in r["events"]].count("turn.end") == 1
    row = [x for x in _metrics(cfg) if x["session_id"] == "SID-CUSH-SLOW"][-1]
    assert row["cushion"] is True and row["outcome"] == "ok"
    # the cushion is the first sound: about cushion_after_s after the last voiced frame
    assert 0.9 <= row["first_audio_ms_rel"] / 1000 <= 1.8


def test_fast_turn_has_no_cushion(agent, two_mocks):
    start, cfg = agent
    cfg.cushion_after_s = 3.0
    cfg.scripted_delay_ms = 50
    cfg.send_transcript_event = True
    (store, murl), _ = two_mocks
    r = _go(start, "SID-CUSH-FAST", murl)
    assert r["turn_end"] and CUSHION not in _said(r)
    row = [x for x in _metrics(cfg) if x["session_id"] == "SID-CUSH-FAST"][-1]
    assert row["cushion"] is False


def test_off(agent, two_mocks):
    start, cfg = agent
    cfg.cushion = False
    cfg.cushion_after_s = 1.0
    cfg.scripted_delay_ms = 2500
    cfg.send_transcript_event = True
    (store, murl), _ = two_mocks
    r = _go(start, "SID-CUSH-OFF", murl)
    assert r["turn_end"] and CUSHION not in _said(r) and any("배송중" in s for s in _said(r))
