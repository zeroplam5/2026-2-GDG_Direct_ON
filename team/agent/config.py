"""Agent configuration.

Every value can be overridden with an environment variable (AGENT_<FIELD>, upper case).
API keys are never stored here: only the *name* of the environment variable that holds
the key is configured (e.g. AGENT_LLM_API_KEY_ENV=UPSTAGE_API_KEY).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEAM_ROOT = HERE.parent
REPO_ROOT = TEAM_ROOT.parent
# Brand-specific files ("packs"). official = 2026-11-21 day kit (default), practice = before that
# (tests about the practice rules use it explicitly, see practice_config()).
OFFICIAL_PACK = HERE / "packs" / "official"
PRACTICE_PACK = HERE / "prompts"
PRACTICE_PATHS = {"prompt_path": str(PRACTICE_PACK / "practice_system_prompt.md"),
                  "rules_path": str(PRACTICE_PACK / "practice_rules.json"),
                  "phrases_path": str(PRACTICE_PACK / "practice_phrases.json")}


def repo_path(p) -> Path:
    """A relative path is taken from the repository root (works from any working directory)."""
    p = Path(p)
    return p if p.is_absolute() else REPO_ROOT / p


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 9000

    # ── end-of-utterance (EOU) detection ───────────────────────────────────
    # vad: "webrtc" (webrtcvad + energy gate) or "energy" (RMS only, like the example server)
    vad: str = "webrtc"
    webrtc_aggressiveness: int = 2         # 0..3
    energy_min_rms: float = 300.0          # frames quieter than this are never speech
    noise_floor_ratio: float = 3.0         # speech must also exceed noise_floor * ratio
    min_speech_ms: int = 160               # voiced run needed to open an utterance (blip filter)
    preroll_ms: int = 300                  # audio kept before speech onset
    stt_trigger_silence_ms: int = 400      # start speculative STT after this much silence
    semantic_eou: bool = True              # allow early commit when the transcript looks complete
    complete_silence_ms: int = 1100        # commit if transcript looks complete (measured sentence gaps: 0.6-0.9 s)
    max_silence_ms: int = 1800             # commit regardless of transcript after this much silence
    max_utterance_ms: int = 30000          # force commit for very long speech

    # ── providers ──────────────────────────────────────────────────────────
    # Defaults = models fixed for the event: gpt-4o-mini-transcribe / gpt-4.1 (or gpt-5, gpt-4o-mini) /
    # gpt-4o-mini-tts. Local providers (faster_whisper, sapi) remain for offline work.
    audio_base_url: str = "https://api.openai.com/v1"   # used by the openai STT/TTS providers
    audio_api_key_env: str = "OPENAI_API_KEY"

    stt_provider: str = "openai"           # openai | soniox | faster_whisper | fake
    stt_model: str = "gpt-4o-mini-transcribe"
    stt_language: str = "ko"
    stt_prompt: str = "건강기능식품 쇼핑몰 고객센터 전화 상담. 주문번호 같은 코드는 영문 대문자와 숫자로 적는다."  # practice hint
    stt_timeout_s: float = 6.0
    stt_hedge_ms: int = 1500               # send a 2nd identical request if the 1st is this slow (0 = off)
    whisper_model: str = "large-v3-turbo"  # faster_whisper provider only
    stt_device: str = "auto"               # faster_whisper: auto | cuda | cpu
    stt_compute_type: str = "auto"
    stt_beam_size: int = 1
    stt_workers: int = 2
    stt_fake_text: str = ""                # only for the fake provider (tests)
    # soniox provider (11/21 official STT). Model names change: check GET https://api.soniox.com/v1/models
    soniox_url: str = "wss://stt-rt.soniox.com/transcribe-websocket"
    soniox_api_key_env: str = "SONIOX_API_KEY"
    soniox_model: str = "stt-rt-v4"
    soniox_language_hints: str = "ko"      # comma separated
    soniox_chunk_ms: int = 100             # utterance audio is sent in chunks of this size, as fast as possible
    # end-of-utterance mode. vad (default) = local VAD + speculative STT + completeness check.
    # soniox_stream (opt-in, NOT tested with a real key) = Soniox streaming "<end>" token decides,
    # see team/agent/soniox_stream.py. Compare both with the same audio before choosing.
    eou_mode: str = "vad"
    soniox_max_endpoint_delay_ms: int = 1500   # Soniox recommended start values (team/docs/ENDPOINTING.md)
    soniox_endpoint_latency: int = 2           # endpoint_latency_adjustment_level 0..3
    soniox_endpoint_sensitivity: float = 0.3   # -1.0..1.0
    soniox_min_silence_ms: int = 300           # our own VAD silence required before acting on "<end>"
    soniox_fallback_silence_ms: int = 3000     # local silence limit if "<end>" never comes (then batch STT)

    llm_provider: str = "openai_compat"    # openai_compat | scripted (test double, NOT an AI)
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-5.4-mini"        # 11/21 official LLM (earlier practice: gpt-4.1 | gpt-5 | gpt-4o-mini)
    llm_api_key_env: str = "OPENAI_API_KEY"
    llm_temperature: float = 0.2           # ignored for reasoning models (gpt-5*, o*)
    llm_max_tokens: int = 300              # reasoning models get max(this, 1024): budget includes reasoning
    llm_token_param: str = "auto"          # auto | max_tokens | max_completion_tokens | none
    llm_reasoning_effort: str = "auto"     # auto -> "none" for gpt-5.x (5.1+), "minimal" for gpt-5 / o*, else not sent
    llm_timeout_s: float = 12.0

    tts_provider: str = "openai"           # openai | gemini | sapi (Windows, local) | tone (test double)
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "coral"
    tts_instructions: str = "차분하고 친절한 한국어 전화 상담원 말투. 또렷하고 약간 빠르게 말한다."
    tts_timeout_s: float = 10.0
    sapi_voice: str = "Korean"             # sapi provider: substring of the voice description
    tts_rate: int = 1                      # SAPI rate -10..10
    tts_workers: int = 2
    # gemini provider (11/21 official TTS). Output is 24 kHz s16le mono PCM per the docs
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_api_key_env: str = "GEMINI_API_KEY"
    gemini_tts_model: str = "gemini-2.5-flash-preview-tts"
    gemini_tts_voice: str = "Kore"
    gemini_tts_style: str = ""             # optional prefix prompt, e.g. "친절한 상담원 말투로 읽어 주세요:" (may be read aloud - test first)
    gemini_tts_stream: bool = False        # streamGenerateContent (not shown in the docs for TTS - test before enabling)
    # test doubles only: simulated latency (tone TTS: base + per character, like a non-streaming TTS)
    tone_delay_ms: int = 20
    tone_delay_per_char_ms: float = 0.0
    scripted_delay_ms: int = 50

    # ── dialog / tools ─────────────────────────────────────────────────────
    # official pack (team/agent/packs/official) by default; practice pack: team/agent/prompts
    prompt_path: str = str(OFFICIAL_PACK / "prompt.md")
    phrases_path: str = str(OFFICIAL_PACK / "phrases.json")
    tool_timeout_s: float = 8.0
    max_tool_rounds: int = 4
    # every rule the code checks (gate, hand-off signals, consent words) lives in this one file
    rules_path: str = str(OFFICIAL_PACK / "rules.json")
    # a pack folder with pack.json sets prompt_path / rules_path / phrases_path at once
    # (practice pack: team/agent/prompts). Individually set AGENT_*_PATH values still win.
    pack_dir: str = ""
    # official brand material (day kit data/brand: scenarios.md, information.json, style.json),
    # appended to the prompt at startup. Not in git (the kit data must not be published).
    # Relative = from the repository root. Missing folder = prompt without brand material (warning).
    brand_dir: str = "data/brand"
    # pre-execution gate (team/agent/gate.py): code checks format, grounding and customer consent
    # before a function call is sent
    tool_gate: bool = True
    # output check (team/agent/output_guard.py) on every model sentence before TTS:
    # log = record numbers found nowhere in prompt / customer words / function results,
    # block = replace such a sentence with phrases["unverified_fallback"], off = skip
    output_guard: str = "log"
    # checks that replace the sentence even in "log" mode (comma list, "" = log only):
    # ids = phone / order numbers found nowhere -> phrases["unverified_id_fallback"]
    # time = contact / booking promised for a time no function result carries
    #        -> phrases["unconfirmed_time_fallback"]
    output_guard_strict: str = "ids,time"
    # latency: say a short pre-synthesized phrase (phrases.json "tool_ack") while functions run,
    # only when the model asked for a function before saying anything in this turn
    tool_ack: bool = False                  # off by default: not decided by the team / official manual yet
    # latency: the turn's first chunk may end at a comma once it has this many characters (0 = off),
    # so TTS starts before the first sentence is finished
    first_chunk_min_chars: int = 0          # off by default; 8 was used in the practice measurement
    turn_budget_s: float = 20.0            # starter-kit evaluator gives up 25 s after speech end

    # ── protocol / output ──────────────────────────────────────────────────
    session_start_timeout_s: float = 10.0
    send_transcript_event: bool = True     # {"type":"agent.transcript"} as in the example server
    output_pacing: str = "burst"           # burst | realtime (unconfirmed which the official harness expects)
    output_chunk_ms: int = 20

    # ── logging ────────────────────────────────────────────────────────────
    metrics_path: str = str(TEAM_ROOT / "runs" / "agent_metrics.jsonl")
    log_text: bool = False                 # write transcripts / replies to the local log (off by default)

    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        cfg = cls()
        explicit = set()
        for f in fields(cls):
            if f.name == "extra":
                continue
            raw = os.environ.get("AGENT_" + f.name.upper())
            if raw is not None:
                setattr(cfg, f.name, _coerce(raw, getattr(cfg, f.name)))
                explicit.add(f.name)
        for k, v in overrides.items():
            if v is not None:
                setattr(cfg, k, v)
                explicit.add(k)
        if cfg.pack_dir:
            cfg.apply_pack(cfg.pack_dir, keep=explicit)
        return cfg

    def apply_pack(self, pack_dir: str, keep: set = frozenset()) -> None:
        """Point prompt / rules / phrases at the files named in <pack_dir>/pack.json,
        except fields listed in `keep` (set explicitly by the user)."""
        import json
        d = Path(pack_dir)
        if not d.is_absolute():
            d = TEAM_ROOT.parent / d
        manifest = json.loads((d / "pack.json").read_text(encoding="utf-8"))
        for key, attr in (("prompt", "prompt_path"), ("rules", "rules_path"), ("phrases", "phrases_path")):
            if manifest.get(key) and attr not in keep:
                setattr(self, attr, str(d / manifest[key]))

    def public_dict(self) -> dict:
        """Config for logs. Contains env-var *names*, never key values."""
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "extra"}


def practice_config(**kw) -> Config:
    """Config on the practice pack (rules / prompt / phrases from before 2026-11-21)."""
    return Config(**{**PRACTICE_PATHS, **kw})


def _coerce(raw: str, default):
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw
