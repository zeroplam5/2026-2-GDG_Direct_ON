"""Cushion phrase: when nothing has been said cushion_after_s after the end of the customer's speech,
the agent says one short phrase once, then the answer. Three phrases are used in turn across calls. Test doubles only (fake STT,
ScriptedLLM with a delay, ToneTTS) - no API keys."""
from team.agent.config import Config

from .helpers import run_client, silence, speechlike
from .test_agent_protocol import _metrics, agent, run, two_mocks  # noqa: F401  (fixtures)

PHRASES = Config().cushion_text.split("|")


def _go(start, sid, tools_url):
    async def go():
        h = await start()
        return await run_client(h["url"], sid, speechlike(1.5) + silence(0.3), tools_url, case_id="ORDER_OK", speed=1)
    return run(go())


def _said(r):
    return [e["text"] for e in r["events"] if e.get("type") == "agent.transcript"]


def test_defaults_and_env(monkeypatch):
    cfg = Config()
    assert cfg.cushion is True and cfg.cushion_after_s == 5.0
    assert PHRASES == ["오래 기다려 주셔서 감사합니다.", "기다리게 해 드려 죄송합니다.", "잠시만 기다려 주세요, 곧 안내해 드리겠습니다."]
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
    assert r["turn_end"] and said[0] in PHRASES and sum(s in PHRASES for s in said) == 1
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
    assert r["turn_end"] and not set(_said(r)) & set(PHRASES)
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
    assert r["turn_end"] and not set(_said(r)) & set(PHRASES) and any("배송중" in s for s in _said(r))


def test_phrases_take_turns_across_calls(agent, two_mocks):
    """Each official case is a new connection to the same server: four slow calls in a row hear
    phrase 1, 2, 3, then 1 again."""
    start, cfg = agent
    cfg.cushion_after_s = 0.6
    cfg.scripted_delay_ms = 1500
    cfg.send_transcript_event = True
    (store, murl), _ = two_mocks

    async def go():
        h = await start()
        out = []
        for i in range(4):
            r = await run_client(h["url"], f"SID-CUSH-ROT-{i}", speechlike(1.0) + silence(0.3), murl,
                                 case_id="ORDER_OK", speed=4)
            out.append(next(s for s in _said(r) if s in PHRASES))
        return out

    assert run(go()) == [PHRASES[0], PHRASES[1], PHRASES[2], PHRASES[0]]


def test_phrases_json_list_wins(tmp_path):
    """A "cushion" list in the phrase file replaces the built-in phrases."""
    import asyncio
    import json

    from team.agent.server import App
    base = Config()
    phrases = json.loads(open(base.phrases_path, encoding="utf-8").read())
    phrases["cushion"] = ["하나입니다.", "둘입니다."]
    f = tmp_path / "phrases.json"
    f.write_text(json.dumps(phrases, ensure_ascii=False), encoding="utf-8")
    app = App(Config(stt_provider="fake", llm_provider="scripted", tts_provider="tone", phrases_path=str(f),
                     metrics_path=str(tmp_path / "m.jsonl")))
    asyncio.run(app.warm())
    assert [app.next_cushion()[0] for _ in range(3)] == ["하나입니다.", "둘입니다.", "하나입니다."]
