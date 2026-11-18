"""Provider factory. Providers are selected by config (AGENT_STT_PROVIDER etc.)."""
from __future__ import annotations


def make_stt(cfg):
    from . import stt
    if cfg.stt_provider == "openai":
        return stt.OpenAISTT(cfg)
    if cfg.stt_provider == "faster_whisper":
        return stt.FasterWhisperSTT(cfg)
    if cfg.stt_provider == "fake":
        return stt.FakeSTT(cfg)
    raise ValueError(f"unknown stt_provider {cfg.stt_provider!r}")


def make_llm(cfg):
    from . import llm
    if cfg.llm_provider == "openai_compat":
        return llm.OpenAICompatLLM(cfg)
    if cfg.llm_provider == "scripted":
        return llm.ScriptedLLM(cfg)
    raise ValueError(f"unknown llm_provider {cfg.llm_provider!r}")


def make_tts(cfg):
    from . import tts
    if cfg.tts_provider == "openai":
        return tts.OpenAITTS(cfg)
    if cfg.tts_provider == "sapi":
        return tts.SapiTTS(cfg)
    if cfg.tts_provider == "tone":
        return tts.ToneTTS(cfg)
    raise ValueError(f"unknown tts_provider {cfg.tts_provider!r}")
