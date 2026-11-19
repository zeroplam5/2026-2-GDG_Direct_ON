"""team.scripts.check_audio with test doubles (no key): wav in / wav out formats."""
import asyncio
import wave

from team.agent.config import Config
from team.scripts.check_audio import check_stt, check_tts


def test_stt_and_tts_round(tmp_path):
    cfg = Config(stt_provider="fake", stt_fake_text="테스트", tts_provider="tone")
    r = asyncio.run(check_stt(cfg, "team/data/practice_ko/audio/K01.wav"))
    assert r["text"] == "테스트" and r["audio_s"] > 1
    out = tmp_path / "t.wav"
    t = asyncio.run(check_tts(cfg, "안녕하세요.", out))
    assert t["even_bytes"] and t["bytes"] > 0
    with wave.open(str(out)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
