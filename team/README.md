# 팀 베이스라인 (DIRECT:ON 2026 사전 연습용)

스타터킷 원본 파일(`README.md`, `docs/`, `example_server/`, `mock_tools/`, `eval/`, `data/`)은 **수정하지 않았습니다.**
팀이 추가한 코드는 모두 이 `team/` 폴더에 있고, 결과·판단 근거는 [docs/REPORT.md](docs/REPORT.md)에 정리했습니다.

> 이 폴더의 프롬프트·정책·매칭 규칙·연습 음성은 **연습용**이며, 공식 매뉴얼·문항·채점 기준이 아닙니다.

> **`prototype` 브랜치**: 기능 브랜치 5개(new-models, latency, llm-gate, measure-ops, endpointing)와 브라우저 테스트 화면을 합친 통합본입니다. 통합 내용·검증 결과·알려진 위험은 [docs/PROTOTYPE.md](docs/PROTOTYPE.md), 11/21 실제 키 검증 절차는 [docs/API_CHECK_1121.md](docs/API_CHECK_1121.md)를 보세요. **모든 기능은 아직 실제 API 키로 검증하지 않았습니다.**

## 설치 (저장소 루트에서)
```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r team/requirements.txt
```
- **맥에서는 `team/requirements.txt`가 설치되지 않습니다** (GPU 패키지에 맥용 파일이 없음). API 모델만 쓸 때는 어느 OS든 `team/requirements-api.txt`를 쓰세요. 환경 변수 안내는 `team/env.example`.
- 측정 당일 절차, 외부 접속·동시 4연결 점검, 제출 압축: [docs/OPS.md](docs/OPS.md) · 문제 사례: [docs/ISSUES.md](docs/ISSUES.md) · `speak()` 검토: [docs/SPEAK.md](docs/SPEAK.md) · 튜닝에 쓰지 않는 테스트셋: [data/holdout_ko](data/holdout_ko/README.md)
- 테스트 환경: Windows 11, CPython 3.12.13(uv), RTX 3070 8GB, i5-10400F. 한국어 TTS는 Windows SAPI의 `Microsoft Heami Desktop` 음성을 사용합니다.
- STT 모델(`large-v3-turbo`)은 처음 실행할 때 Hugging Face에서 내려받습니다(약 1.6GB, 이 PC에서는 약 5분).

## 실행 (터미널 3개)
```bash
# A. 연습용 모의 함수 서버 (lookup 구현)
.venv/Scripts/python -m team.mock.mock_api --port 8100 [--data 경로 --tools 경로 --match normalized|strict]

# B. 상담원 서버 (저장소 루트의 .env를 자동으로 읽음. 이미 있는 환경 변수가 우선. --env-file ''이면 끔)
#   기본값 = 고정 모델: STT gpt-4o-mini-transcribe / LLM gpt-4.1 / TTS gpt-4o-mini-tts (키: OPENAI_API_KEY)
.venv/Scripts/python -m team.agent.server --port 9000
#   LLM 교체: AGENT_LLM_MODEL=gpt-5 또는 gpt-4o-mini
#   (gpt-5 계열은 max_completion_tokens와 reasoning_effort=minimal을 자동 적용)
#   오프라인: --stt faster_whisper --tts sapi
#   키 없이 파이프라인만 확인: 스크립트 LLM 대체물 사용 (AI가 아님)
.venv/Scripts/python -m team.agent.server --port 9000 --llm scripted

# C. 스타터킷 측정기 그대로 사용 (Windows에서 한글 케이스 파일을 읽으려면 PYTHONUTF8=1 필요)
PYTHONUTF8=1 .venv/Scripts/python eval/run_eval.py --server ws://127.0.0.1:9000 \
  --cases team/data/practice_ko/cases.jsonl --audio team/data/practice_ko/audio \
  --mock http://127.0.0.1:8100 --out team/runs/my_run
```
## 브라우저 음성 테스트 화면 (팀 내부 개발 도구)
```bash
.venv/Scripts/python -m team.mock.mock_api --port 8100
.venv/Scripts/python -m team.devui.server          # http://127.0.0.1:9100 에서 직접 말하고 응답 듣기
```
대회용 서버(9000)와 같은 파이프라인을 쓰고, 인식 텍스트·함수 인자/결과·발화 종료 판단·시간을 `/ws` 경로에서만 추가로 보여 줍니다. 자세한 내용은 [devui/README.md](devui/README.md)를 보세요.

## 11/21 공식 모델로 실행 (Soniox STT · Gemini 2.5 Flash TTS · gpt-5.4-mini)
운영진 공지(11/19)로 모델이 바뀌었습니다. 기본값은 아직 예전 모델(OpenAI 3종)이라 기존 실행 방법은 그대로 동작하고, 아래 환경 변수로 새 모델을 켭니다.
```bash
# .env (키 값은 커밋 금지)
SONIOX_API_KEY=...
GEMINI_API_KEY=...
OPENAI_API_KEY=...
AGENT_STT_PROVIDER=soniox
AGENT_TTS_PROVIDER=gemini
AGENT_LLM_MODEL=gpt-5.4-mini
```
먼저 키와 형식을 점검합니다 (Gemini로 합성한 문장을 Soniox로 다시 받아쓰고, gpt-5.4-mini 함수 호출 1회):
```bash
.venv/Scripts/python -m team.scripts.check_llm --audio --model gpt-5.4-mini
```
- **실제 키로는 아직 시험하지 않았습니다.** 문서의 형식을 흉내 낸 가짜 서버로만 확인했습니다(`team/tests/test_new_providers.py`).
- Soniox: 지금 구조(무음 400ms에 미리 STT)에 맞춰, 호출마다 WebSocket을 열고 발화 음성을 한꺼번에 보낸 뒤 최종 글자를 받습니다. 모델명은 자주 바뀌므로 `AGENT_SONIOX_MODEL`로 바꿉니다(`GET https://api.soniox.com/v1/models`로 확인).
- Gemini TTS: 문서상 24 kHz PCM → 16 kHz로 리샘플합니다. 스트리밍(`AGENT_GEMINI_TTS_STREAM=1`)은 문서에 TTS 예시가 없어 기본은 꺼 두었습니다. 첫 소리 지연을 재 보고 켤지 정합니다.
- gpt-5.4-mini: 추론 모델로 분류되어 `max_completion_tokens`를 쓰고, `reasoning_effort`는 `none`을 보냅니다(5.1 이후 모델 목록에 `minimal`이 없음). Chat Completions 지원 여부는 위 점검으로 확인해야 합니다.

| 설정 (`AGENT_` 접두사) | 기본값 |
|---|---|
| `SONIOX_MODEL`, `SONIOX_LANGUAGE_HINTS`, `SONIOX_API_KEY_ENV`, `SONIOX_URL`, `SONIOX_CHUNK_MS` | stt-rt-v4, ko, SONIOX_API_KEY, wss://stt-rt.soniox.com/transcribe-websocket, 100 |
| `GEMINI_TTS_MODEL`, `GEMINI_TTS_VOICE`, `GEMINI_TTS_STYLE`, `GEMINI_TTS_STREAM`, `GEMINI_API_KEY_ENV` | gemini-2.5-flash-preview-tts, Kore, (없음), 0, GEMINI_API_KEY |

## 테스트 방법 (4단계)
```bash
# 1) 자동 테스트: 키와 네트워크 없이 프로토콜, 모의 서버, 동시성, 오류 처리 확인 (약 50초, 98개 · 그중 3개는 알려진 한계를 표시하는 xfail)
.venv/Scripts/python -m pytest team/tests -q
node --test team/devui/tests/pcm.test.mjs team/devui/tests/score.test.mjs   # 브라우저 음성 변환 12개 + 연습 점수 계산 8개

# 2) LLM 키 사전 점검: 키 존재 여부, 모델 목록, 함수 호출 1회 (키 값은 출력하지 않음)
.venv/Scripts/python -m team.scripts.check_llm --list
.venv/Scripts/python -m team.scripts.check_llm --model gpt-4.1
.venv/Scripts/python -m team.scripts.check_llm --audio      # TTS→STT 왕복 확인

# 3) 전체 측정: 모의 서버를 먼저 띄운 뒤 실행. 에이전트를 자동으로 띄우고 원본 측정기를 반복 실행해 p50/p90 집계
.venv/Scripts/python -m team.mock.mock_api --port 8100 &
.venv/Scripts/python -m team.scripts.bench --llm openai_compat --configs default_1100_1800 --repeats 3 \
  --out team/runs/my_bench \
  --agent-env "AGENT_LLM_MODEL=gpt-4.1"
#    결과: team/runs/my_bench/summary.json (지연과 끼어듦)
#          team/runs/my_bench/<config>/rep*/results.jsonl (응답 텍스트와 함수 호출)
#    발화 종료 설정 비교: --configs example_rule_800,semantic_700_1600,default_1100_1800,silence_only_1500

# 4) 수동 1회 측정: 위의 "실행 (터미널 3개)"
```
- 측정 중 `bench.py`는 `AGENT_LOG_TEXT=1`을 켜므로, `agent_metrics.jsonl`에 인식 텍스트와 응답이 남습니다.
- 실제 LLM으로 측정하면 STT 텍스트, 발신자 정보, 이력이 LLM 제공사로 전송됩니다.
- 연습 음성 다시 만들기: `.venv/Scripts/python -m team.scripts.make_practice_audio`

## 주요 설정 (환경 변수 `AGENT_<이름>`, 기본값은 [agent/config.py](agent/config.py) 참고)
| 구분 | 설정 | 기본값 |
|---|---|---|
| 발화 종료 | `VAD`, `COMPLETE_SILENCE_MS`, `MAX_SILENCE_MS`, `SEMANTIC_EOU`, `STT_TRIGGER_SILENCE_MS` | webrtc, 1100, 1800, 1, 400 |
| STT | `STT_PROVIDER`, `STT_MODEL`, `STT_PROMPT`, `STT_HEDGE_MS` | openai, gpt-4o-mini-transcribe, 연습용 힌트, 1500 |
| LLM | `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY_ENV` | openai_compat, OpenAI, gpt-4.1, OPENAI_API_KEY |
| TTS | `TTS_PROVIDER`, `TTS_MODEL`, `TTS_VOICE`, `TTS_INSTRUCTIONS` | openai, gpt-4o-mini-tts, coral, 연습용 말투 |
| 응대 | `PROMPT_PATH`, `PHRASES_PATH`, `TURN_BUDGET_S`, `TOOL_TIMEOUT_S` | prompts/practice_*, 20, 8 |
| 함수·출력 검사 | `TOOL_GATE`(실행 전 관문), `OUTPUT_GUARD`(log=근거 없는 숫자 기록만·반복 문장 버림 / block / off), `PACK_DIR`(공식 규칙 묶음 폴더) [docs/LLM_GATE.md](docs/LLM_GATE.md) | 1, log, (없음) |
| 말 끝 판단 모드 | `EOU_MODE`: vad(기존) / soniox_stream(Soniox `<end>`, **실제 키 미검증**) [docs/PROTOTYPE.md](docs/PROTOTYPE.md) | vad |
| 지연 줄이기 | `TOOL_ACK`(조회 중 멘트), `FIRST_CHUNK_MIN_CHARS`(첫 조각을 쉼표에서 끊기, 0=끔) — **기본 꺼짐, 팀 결정 후 켬** [docs/LATENCY.md](docs/LATENCY.md) | 0, 0 |
| 출력 | `OUTPUT_PACING`, `SEND_TRANSCRIPT_EVENT`, `LOG_TEXT` | burst, 1, 0 |

API 키 값은 코드와 로그에 들어가지 않습니다. 설정에는 키가 담긴 **환경 변수의 이름**만 적습니다.

## 파일 구성
```
team/
  agent/
    server.py         WebSocket 진입점, 공급자 초기화, 고정 문구 미리 합성
    session.py        연결 1개 = Session 1개: 발화 종료 판단 → 응답 → turn.end (세션별 상태 분리)
    endpointing.py    프레임 단위 VAD(webrtcvad + 에너지·잡음 하한), 발화 버퍼
    textutil.py       한국어 문장 완결 추정, 문장 단위 분리(스트리밍 TTS), TTS용 숫자 읽기
    dialog.py         LLM ↔ 함수 호출 반복, 문장 스트리밍
    tools_client.py   세션별 tools_url·X-Session-Id 사용, 정의되지 않은 함수 차단, 오류를 결과로 변환
    policy.py         시스템 프롬프트 조립, 발신자·이력 컨텍스트, 인계 후보 키워드 (연습용)
    config.py         모든 설정 (환경 변수)
    audio.py          PCM 처리, 리샘플링, 프레임 재조립
    providers/        stt.py(openai | soniox | faster_whisper | fake), llm.py(openai_compat | scripted), tts.py(openai | gemini | sapi | tone)
    prompts/          practice_system_prompt.md, practice_phrases.json  ← 11/21 공식 매뉴얼로 교체할 파일
  devui/              브라우저 음성 테스트 화면 + 테스트 서버 (개발용, 대회 경로와 분리)
  mock/mock_api.py    lookup()을 구현한 모의 함수 서버 (원본 골격과 같은 경로·응답 형식)
  scripts/            make_practice_audio.py(한국어 연습 음성), bench.py(설정별 반복 측정), check_audio.py(STT·TTS 단독 점검),
                      llm_probe.py(LLM·관문), check_concurrency.py / check_external.py(운영 점검), sweep_endpoint.py(말 끝 실험)
  tests/              pytest (모의 서버, 프로토콜, 동시성, 오류, 발화 종료, LLM·Soniox·Gemini 연동 형식, 지연 줄이기, 관문, devui, Soniox 스트리밍 모드)
  data/practice_ko/   팀이 만든 한국어 연습 음성 8개 + meta.json + cases.jsonl (공식 문항 아님)
  runs/               측정 결과 (bench_scripted/summary.json 등)
  docs/REPORT.md      결과 보고서
```
