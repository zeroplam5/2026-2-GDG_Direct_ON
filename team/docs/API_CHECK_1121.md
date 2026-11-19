# 11/21 공식 키·자료를 받은 뒤 실제 API 검증 절차

2026-11-20 작성 · 대상: 팀장(그대로 따라 실행) · 브랜치 `prototype`

**지금까지의 모든 검증은 키 없이 대체물(가짜 STT·스크립트 LLM·신호음 TTS·가짜 Soniox 서버)로 한 것입니다.**
Soniox, Gemini TTS, gpt-5.4-mini는 실제로 한 번도 호출하지 않았습니다. 아래 순서대로 처음 확인합니다.

원칙
- 비용·크레딧을 아끼려고 **짧은 연습 음성 1개 → 1회 요청**부터 시작하고, 앞 단계가 통과해야 다음 단계로 갑니다.
- 반복 측정(`--repeats`)은 6단계 이후에만 늘립니다. 기본은 1회입니다.
- 키는 저장소 루트의 `.env`에만 둡니다(`.gitignore`에 포함). 화면·로그·커밋에 키 값을 붙여 넣지 않습니다.
- 공식 시험 음성·문항 텍스트를 파일로 저장하지 않습니다. `AGENT_LOG_TEXT=0`(기본)을 유지합니다.
- 명령은 저장소 루트에서 실행합니다(Windows 예시. 맥·리눅스는 `.venv/bin/python`).

## 0. 준비 (1분, 비용 없음)
```bash
git switch prototype
cp team/env.example .env          # 이미 .env가 있으면 아래 줄만 추가
# .env에 값 입력: OPENAI_API_KEY, SONIOX_API_KEY, GEMINI_API_KEY
# 그리고 공식 모델 선택 3줄의 # 제거:
#   AGENT_STT_PROVIDER=soniox
#   AGENT_TTS_PROVIDER=gemini
#   AGENT_LLM_MODEL=gpt-5.4-mini
.venv/Scripts/python -m pytest team/tests -q     # 키 없이 도는 테스트가 그대로 통과하는지
```
키를 운영진이 회사별로 주는지, 하나의 플랫폼(예: OpenRouter 같은 중계)으로 주는지에 따라 `AGENT_LLM_BASE_URL`, `AGENT_LLM_API_KEY_ENV`가 달라집니다. 중계 방식이면 [LLM_GATE.md](LLM_GATE.md)의 OpenRouter 설정 예를 참고합니다.

## 1. Soniox STT 단독 (요청 1회)
```bash
.venv/Scripts/python -m team.scripts.check_audio --stt team/data/practice_ko/audio/K01.wav
```
- 통과: `"provider": "soniox"`, 인식 텍스트가 "주문번호는 데모 일공공일이에요…"와 비슷함, 오류 없음.
- 실패하면 먼저 볼 것
  - 인증 오류: 지금 코드는 **첫 config 메시지의 `api_key`** 로 인증합니다. `endpoint_server.py`(실험 서버)는 `Authorization: Bearer` 헤더를 썼습니다. 둘 중 하나만 맞을 수 있습니다.
  - 모델 이름 오류: `AGENT_SONIOX_MODEL`을 바꿉니다(`GET https://api.soniox.com/v1/models`로 확인).
- 기록할 것: `provider_ms`(발화 6초를 한꺼번에 보내고 최종 글자를 받기까지). 지금 Soniox 연결은 **호출마다 WebSocket을 새로 여는 일괄 방식**입니다. 발화 중 계속 인식하는 방식이 아닙니다.

## 2. Gemini TTS 단독 + 재생 (요청 1회)
```bash
.venv/Scripts/python -m team.scripts.check_audio --tts --play
```
- 통과: `"provider": "gemini"`, `even_bytes: true`, `audio_s`가 문장 길이에 맞음(약 3~5초), 재생했을 때 **속도·높이가 정상**(24 kHz → 16 kHz 변환이 맞다는 뜻), 잘림·잡음 없음.
- 기록할 것: `first_audio_ms`(첫 소리까지), `total_ms`.
- 선택(요청 1회 더): `AGENT_GEMINI_TTS_STREAM=1`로 같은 명령을 다시 실행해 `chunks`가 2개 이상이고 `first_audio_ms`가 줄어드는지 봅니다. 문서에 TTS 스트리밍 예시가 없어 기본은 꺼 두었습니다. 줄어들지 않거나 오류가 나면 0으로 둡니다.
- 1·2단계를 한 번에 보고 싶으면 `check_llm --audio`(Gemini로 합성 → Soniox로 받아쓰기)를 씁니다.

## 3. gpt-5.4-mini 일반 응답·함수 호출 (요청 2~4회)
```bash
.venv/Scripts/python -m team.scripts.check_llm --model gpt-5.4-mini                 # 함수 호출 1회, 키 값은 출력 안 함
.venv/Scripts/python -m team.scripts.llm_probe --case K07 --repeat 1                # 관문이 확인 없는 콜백을 막는지
.venv/Scripts/python -m team.scripts.llm_probe --case K01 --repeat 1 --full         # 조회 → 결과 뒤 답까지
```
- 통과: 오류 없음, K01은 `get_order_info(order_id=DEMO1001)` 호출, K07은 관문이 `request_callback`을 막고 확인 질문을 함.
- 실패하면 먼저 볼 것: `reasoning_effort=none`이 거절되는지, Chat Completions에서 이 모델을 받는지, `max_completion_tokens`.

## 4. 실제 API 전체 단일 세션 (요청: STT 1 + LLM 1~2 + TTS 2~3)
터미널 3개:
```bash
# A. 모의 함수 서버
.venv/Scripts/python -m team.mock.mock_api --port 8100
# B. 상담원 서버 (대회용 경로, 9000)
.venv/Scripts/python -m team.agent.server --port 9000
# C. 스타터킷 측정기, 1문항만, 동시 1
PYTHONUTF8=1 .venv/Scripts/python eval/run_eval.py --server ws://127.0.0.1:9000 \
  --cases team/data/practice_ko/cases.jsonl --audio team/data/practice_ko/audio \
  --mock http://127.0.0.1:8100 --out team/runs/real_single --limit 1 --concurrency 1
```
- 통과: `turn.end` 수신, 끼어듦 없음, 함수 서버 기록에 `get_order_info`가 그 세션 ID(`X-Session-Id`)로 남음, 응답이 실제 AI 문장.
- 기록할 것: `team/runs/agent_metrics.jsonl`의 `commit_ms_rel`, `stt_ms`, `llm_first_text_ms_rel`, `first_audio_ms_rel`, `turn_end_ms_rel`.
- 브라우저로도 확인: B 대신 `.venv/Scripts/python -m team.devui.server` → http://127.0.0.1:9100 에서 직접 한 번 말하기. 화면의 STT·LLM·TTS 줄에 soniox / openai_compat(gpt-5.4-mini) / gemini가 보여야 합니다.

## 5. 경계 상황 (각 1회, 브라우저 화면 또는 측정기)
| 상황 | 방법 | 볼 것 |
|---|---|---|
| 긴 쉼 | 측정기 `--limit 5` (K04·K05 포함) 또는 화면에서 "주문번호가 … (1.5초) … 데모 일공공일" | 끼어듦 여부, `eou_reason` |
| 숫자 정정 | K08, 화면에서 "데모 일공공일, 아니 데모 일공공이요" | 함수 카드의 실제 `order_id` |
| 함수 실패 | 화면 설정에서 `tools_url`을 `http://127.0.0.1:8199`(아무것도 없는 주소)로 | `error_fallback` 안내, 지어낸 결과 없음 |
| 동의 없는 요청 | K07, 화면에서 "내일 다시 전화 주실 수 있나요?" | 함수 카드에 "실행 전 관문에서 차단", 함수 서버 기록 0건 |
| 말 끝 판단 비교 (선택) | 아래 명령 | 끼어듦 수, 첫 소리 |

말 끝 판단 비교는 같은 음성 18개로 기존 방식과 Soniox 스트리밍 방식을 견줍니다(요청 약 36세션 분량, 마지막에). `bench`는 상담원 서버를 직접 띄우지만 모의 함수 서버(8100)는 먼저 켜 두어야 합니다:
```bash
.venv/Scripts/python -m team.scripts.bench --llm openai_compat --configs default_1100_1800 --repeats 1 \
  --cases team/data/endpoint_ko/cases.jsonl --audio team/data/endpoint_ko/audio --out team/runs/eou_vad
.venv/Scripts/python -m team.scripts.bench --llm openai_compat --configs default_1100_1800 --repeats 1 \
  --cases team/data/endpoint_ko/cases.jsonl --audio team/data/endpoint_ko/audio --out team/runs/eou_soniox \
  --agent-env "AGENT_EOU_MODE=soniox_stream"
```
`AGENT_EOU_MODE=soniox_stream`은 가짜 Soniox 서버로만 시험했습니다. 결과를 보기 전까지 기본값(`vad`)을 바꾸지 않습니다.

## 6. 실제 API 4개 동시 세션
```bash
.venv/Scripts/python -m team.scripts.check_concurrency --real --repeats 1 --out team/runs/concurrency_real
```
- 통과: 끼어듦·오류·`turn.end` 누락 0, 4개 동시의 첫 소리가 1개일 때보다 크게 늘지 않음, 429(속도 제한) 없음.
- 실제 모델은 답이 매번 달라서 "답 내용이 1개씩일 때와 다름" 수치는 참고만 합니다.

## 7. 다른 기기에서 두 서버 주소 접속
[OPS.md](OPS.md) 1장 절차. 요점:
```bash
.venv/Scripts/python -m team.mock.mock_api --host 0.0.0.0 --port 8100     # --host 0.0.0.0 필수
.venv/Scripts/python -m team.agent.server --port 9000
# 다른 노트북에서:
.venv/Scripts/python -m team.scripts.check_external --agent ws://<서버PC IP>:9000 --mock http://<서버PC IP>:8100
```
`0.0.0.0`으로 열었다고 밖에서 접속된다는 보장은 없습니다(방화벽, 와이파이 격리). **반드시 다른 기기에서** 1~5 통과를 확인하고, 막히면 터널을 씁니다.
devui 화면(9100)은 운영진에게 주는 주소가 아닙니다.

## 8. 공식 매뉴얼·함수 정의·모의 데이터 반영 후 재측정
1. [prompts/README.md](../agent/prompts/README.md)의 8단계 체크리스트로 공식 묶음 폴더를 만들고 `AGENT_PACK_DIR`로 켭니다. 공식 함수는 **모두** 규칙 파일 `functions`에 적습니다(빠지면 조회도 "확인 필요"로 막힘).
2. 공식 `session.start.history` 형식을 보고 `team/agent/policy.py`의 `normalize_history` 한 함수만 고칩니다. 화면(devui)의 history 추가 형식(`role`/`text`)도 같이 맞춥니다.
3. 모의 서버를 공식 파일로 다시 띄웁니다: `--data 받은파일.json --tools 공식함수정의.json`.
4. 다시 실행: `pytest` → 3단계(`llm_probe --suite gate --repeat 1`) → 4단계 → 6단계.
5. 공식 매뉴얼이 채움말·대기 멘트를 허용하는지 확인한 뒤에 `AGENT_TOOL_ACK`, `AGENT_FIRST_CHUNK_MIN_CHARS`를 켤지 정하고, 켠 것과 끈 것을 같은 조건으로 비교합니다([LATENCY.md](LATENCY.md)의 명령).
6. 큰 변경 뒤 한 번만 holdout 세트로 확인합니다([holdout_ko](../data/holdout_ko/README.md)).

## 결과 기록 양식 (단계마다 한 줄)
| 단계 | 날짜·시각 | 통과/실패 | 핵심 수치 | 메모 |
|---|---|---|---|---|
| 1 Soniox 단독 | | | provider_ms | |
| 2 Gemini 단독 | | | first_audio_ms / 재생 정상 | |
| 3 gpt-5.4-mini | | | K01·K07 결과 | |
| 4 전체 1세션 | | | first_audio_ms_rel | |
| 5 경계 상황 | | | 끼어듦·관문 | |
| 6 동시 4 | | | p50/p90, 429 | |
| 7 다른 기기 | | | check_external 1~5 | |
| 8 공식 자료 반영 | | | | |
