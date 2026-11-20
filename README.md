# DIRECT:ON 2026 팀 음성 상담원 (`prototype` 브랜치)

전화 상담원 AI입니다. 고객 음성(16 kHz PCM)을 WebSocket으로 받아 **듣기(STT) → 답 만들기(LLM)·함수 호출 → 말하기(TTS)** 를 거쳐 음성으로 답하고 `turn.end`를 보냅니다.
팀이 만든 코드는 모두 `team/` 폴더에 있습니다. 스타터킷 원본(`docs/`, `example_server/`, `mock_tools/`, `eval/`, `data/`)은 고치지 않았고, 스타터킷 원본 README는 [docs/startREADME.md](docs/startREADME.md)로 옮겼습니다.

> **지금 상태 (2026-11-20)**
> - 기능 브랜치 5개와 브라우저 테스트 화면을 합친 통합본입니다 ([docs/INTEGRATION_REPORT.md](team/docs/INTEGRATION_REPORT.md)).
> - **대회 공식 모델(Soniox·Gemini TTS·gpt-5.4-mini)은 아직 실제 키로 시험하지 않았습니다.** 키는 11/21에 받습니다.
> - 프롬프트·규칙·연습 음성·모의 데이터는 모두 **연습용**입니다. 공식 매뉴얼·문항·채점 기준이 아닙니다.

---

## 빠른 시작: 처음 받는 팀원은 이 순서대로

### 0. 준비물
- Python **3.12** 와 [uv](https://docs.astral.sh/uv/) (없으면 `pip install uv`)
- **Chrome 또는 Edge** (브라우저 테스트 화면용)
- Git, (선택) Node.js: 브라우저 쪽 자동 테스트용
- OpenAI 키: **실제 AI 답변**을 들을 때만 필요. 키 없이도 흐름은 시험할 수 있습니다.

### 1. 코드 받기
```powershell
git clone git@github.com:zeroplam5/2026-2-GDG_Direct_ON.git
cd 2026-2-GDG_Direct_ON
git switch prototype
```
이미 받아 두었다면 `git fetch` 후 `git switch prototype` → `git pull`.

### 2. 설치 (저장소 루트에서, 한 번만)

**Windows**
```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r team/requirements.txt
```

**macOS / Linux** (`team/requirements.txt`의 GPU 패키지는 맥에 설치되지 않습니다)
```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r team/requirements-api.txt
uv pip install --python .venv/bin/python faster-whisper==1.2.1    # 키 없이 음성 인식을 하려면 (CPU)
```
- 맥·리눅스에서는 아래 모든 명령의 `.venv/Scripts/python`을 **`.venv/bin/python`** 으로 바꿉니다.
- 로컬 음성 인식 모델(`large-v3-turbo`, 약 1.6 GB)은 처음 실행할 때 자동으로 내려받습니다(수 분). GPU가 없으면 CPU로 동작하고 조금 느립니다.

### 3. 키 파일 (실제 AI 답변을 들을 때만)
```powershell
copy team\env.example .env        # 맥·리눅스: cp team/env.example .env
```
`.env`를 열어 `OPENAI_API_KEY=` 뒤에 키를 넣습니다. **`.env`는 커밋되지 않습니다**(`.gitignore`). 키를 채팅·문서·커밋에 붙여 넣지 마세요.

### 4. 실행: 터미널 2개

**터미널 A: 모의 함수 서버** (항상 먼저, 끄지 않고 둠)
```powershell
.venv/Scripts/python -m team.mock.mock_api --port 8100
```

**터미널 B: 테스트 서버** (아래 중 하나)
```powershell
# (1) 키 없이: 실제 음성 인식 + 대체물 답(정해진 문장)과 신호음
.venv/Scripts/python -m team.devui.server --stt faster_whisper --llm scripted --tts tone

# (2) 실제 AI 답변 (OpenAI 키 필요, 요청마다 비용), 목소리는 Windows 한국어 음성
.venv/Scripts/python -m team.devui.server --stt faster_whisper --llm openai_compat --tts sapi
#     맥·리눅스는 --tts sapi 대신 --tts openai (비용) 또는 --tts tone (신호음)
```
로그에 **`voice test UI: http://127.0.0.1:9100/`** 가 나오면 준비 완료입니다.

### 5. 브라우저에서
1. Chrome/Edge로 **http://127.0.0.1:9100** 을 엽니다. (IP 주소로 열면 브라우저가 마이크를 막습니다)
2. 맨 위 **마이크 자동 찾기** → 마이크 권한 허용 → **1.5초 조용히** 있다가 **3초 동안 계속 말하기**. 목소리가 들어오는 장치가 자동으로 선택됩니다.
3. **테스트 시작** → "마이크 전송 중 · 말씀하세요"가 뜨면 한 번 말하고 기다립니다. 말 끝은 서버가 판단합니다.
4. 결과 보기
   - **대화**: 서버가 알아들은 내 말과 상담원 답
   - **함수 호출**: 보낸 함수·인자·결과, 실행 전 관문에 막힌 호출
   - **평가 기준 · 예상 점수**: 속도 점수 자동 계산, 응대·함수를 직접 판정하면 20점 환산까지 계산 (연습용 추정)
   - **시간**: 말 끝 → 첫 응답 소리까지의 단계별 시간
5. 다음 테스트는 **테스트 시작**을 다시 누릅니다. 이어지는 대화로 시험하려면 먼저 **방금 결과를 history에 추가**.

**바로 해 볼 문장**

| 말할 문장 | 기대 결과 |
|---|---|
| "주문번호 데모 일공공일 배송 어디쯤이에요?" | `get_order_info(DEMO1001)` → 배송중 |
| "주문번호 데모 이공공이 조회해 주세요" | `get_order_info(DEMO2002)` → 배송완료 |
| "주문번호 데모 구구구구 조회해 주세요" | 없는 주문 → "확인되는 내역 없음" |
| "내일 다시 전화 주실 수 있나요?" (실제 AI 모드) | 관문이 콜백 접수를 막고 번호 확인 질문 |

연습용 함수는 `get_order_info`(주문 조회)와 `request_callback`(콜백 접수) 두 개뿐이고, 주문번호는 모두 `DEMO`로 시작합니다. 자세한 사용법은 [devui/README.md](team/devui/README.md).

---

## 실행 모드 한눈에

| 하고 싶은 것 | 터미널 B 명령 | 키 | 비고 |
|---|---|---|---|
| 흐름만 빠르게 (마이크 없이도) | `team.devui.server --stt fake --llm scripted --tts tone` | 없음 | `fake`는 소리를 듣지 않음. `$env:AGENT_STT_FAKE_TEXT="주문번호 데모 일공공일 배송 어디쯤이에요?"` 를 먼저 설정 |
| 내 목소리로 시험, 답은 대체물 | `team.devui.server --stt faster_whisper --llm scripted --tts tone` | 없음 | 답은 정해진 문장, 목소리는 신호음 |
| 실제 AI 답변 | `team.devui.server --stt faster_whisper --llm openai_compat --tts sapi` | OpenAI | 기본 LLM gpt-4.1 |
| 공식 LLM으로 | 위 명령 + `--llm-model gpt-5.4-mini` | OpenAI | 아직 실제로 호출해 본 적 없음 |
| 운영진 측정기가 붙는 대회용 서버 | `team.agent.server --port 9000` | `.env` 설정대로 | 디버그 정보 없음. 아래 "측정기로 속도 재기" |

모든 명령 앞에 `.venv/Scripts/python -m` 을 붙입니다. 대체물(`fake`, `scripted`, `tone`)은 AI가 아닙니다. 화면에도 경고가 뜹니다.

## 측정기로 속도 재기 (공식 측정에 가까운 값)
터미널 A(모의 서버)를 켠 채로:
```powershell
# 터미널 B: 대회용 서버 (.env 설정 사용, 기본 = OpenAI STT·gpt-4.1·OpenAI TTS → 비용 발생)
.venv/Scripts/python -m team.agent.server --port 9000

# 터미널 C: 스타터킷 측정기 → 속도 요약
$env:PYTHONUTF8=1          # 맥·리눅스: export PYTHONUTF8=1
.venv/Scripts/python eval/run_eval.py --server ws://127.0.0.1:9000 --cases team/data/practice_ko/cases.jsonl --audio team/data/practice_ko/audio --mock http://127.0.0.1:8100 --out team/runs/check_speed --concurrency 1
.venv/Scripts/python eval/score_demo.py --run team/runs/check_speed
```
- `score_demo`는 첫 소리 점수만 계산합니다. 함수 문항은 `results.jsonl`의 `after_tool_sound`(조회 뒤 답)도 확인합니다.
- 키 없이 파이프라인만: `team.agent.server --port 9000 --llm scripted`
- 반복 측정·설정 비교: `team.scripts.bench` ([docs/LATENCY.md](team/docs/LATENCY.md))

## 11/21 공식 모델로 실행 (Soniox STT · Gemini 2.5 Flash TTS · gpt-5.4-mini)
`.env`에 키 3개와 모델 선택 3줄을 넣습니다(예시는 `team/env.example`).
```bash
SONIOX_API_KEY=...
GEMINI_API_KEY=...
OPENAI_API_KEY=...
AGENT_STT_PROVIDER=soniox
AGENT_TTS_PROVIDER=gemini
AGENT_LLM_MODEL=gpt-5.4-mini
```
**키를 받으면 [docs/API_CHECK_1121.md](team/docs/API_CHECK_1121.md)를 순서대로 따릅니다.** 비용을 아끼려고 요청 1회짜리 점검부터 시작합니다.
```powershell
.venv/Scripts/python -m team.scripts.check_audio --stt team/data/practice_ko/audio/K01.wav   # Soniox 단독
.venv/Scripts/python -m team.scripts.check_audio --tts --play                               # Gemini 단독 + 재생
.venv/Scripts/python -m team.scripts.check_llm --model gpt-5.4-mini                         # LLM 함수 호출 1회
```
- Soniox는 지금 호출마다 WebSocket을 열고 발화 전체를 한꺼번에 보냅니다. 스트리밍 말 끝 판단은 선택 모드(`AGENT_EOU_MODE=soniox_stream`, 가짜 서버로만 시험).
- Gemini TTS는 24 kHz PCM을 16 kHz로 바꿉니다. 스트리밍(`AGENT_GEMINI_TTS_STREAM=1`)은 기본 꺼짐.
- gpt-5.4-mini에는 `max_completion_tokens`와 `reasoning_effort=none`을 보냅니다.

## 자주 막히는 문제

| 증상 | 원인 | 해결 |
|---|---|---|
| "잘 듣지 못했습니다"만 나옴 | `--stt fake`는 마이크를 듣지 않음 | `--stt faster_whisper`로 다시 실행 |
| 내 말이 "한글자막 by …" 같은 엉뚱한 문장으로 인식됨 | 잡음만 들어가는 장치가 선택됨 (Whisper가 문장을 지어냄) | **마이크 자동 찾기**, 또는 설정 카드의 마이크 점검 |
| 마이크 장치 목록이 비어 있음 | Windows가 브라우저의 마이크 접근을 막음 | Windows 설정 → 개인 정보 및 보안 → 마이크 → 데스크톱 앱 접근 켜기 |
| 마이크 권한을 묻지 않거나 막힘 | IP 주소로 접속 | `http://127.0.0.1:9100` 또는 `http://localhost:9100` 으로 열기 |
| 함수가 한 번도 호출되지 않음 | 모의 서버(8100)가 꺼짐, 주문번호 없이 질문, 또는 `--llm scripted`(콜백은 안 부름) | 함수 호출 카드에 "사용 가능 함수: get_order_info, request_callback"이 보이는지 확인 |
| 서버를 고쳤는데 화면이 그대로 | 서버를 재시작하지 않음 | 터미널 B를 Ctrl+C로 끄고 다시 실행 → 브라우저 새로고침 |
| 측정기에서 한글 파일 읽기 오류 | Windows 기본 인코딩 | `$env:PYTHONUTF8=1` 을 먼저 설정 |
| 포트 사용 중 오류 | 이전 서버가 아직 떠 있음 | 그 터미널을 끄거나 `--port`를 바꿈 (화면 설정의 URL도 같이) |
| `--tts sapi` 오류 (맥·리눅스) | SAPI는 Windows 전용 | `--tts tone` 또는 `--tts openai` |

## 자동 테스트 (키·네트워크 없음)
```powershell
.venv/Scripts/python -m pytest team/tests -q                                       # 약 50초, 98개 (3개는 알려진 한계 xfail)
node --test team/devui/tests/pcm.test.mjs team/devui/tests/score.test.mjs          # 브라우저 음성 변환 12개 + 점수 계산 8개
```
LLM 키 사전 점검(키 값은 출력하지 않음): `team.scripts.check_llm --list`, `--model gpt-4.1`, `--audio`.
LLM·관문 확인 도구: `team.scripts.llm_probe --case K07` ([docs/LLM_GATE.md](team/docs/LLM_GATE.md)).

## 주요 설정 (환경 변수 `AGENT_<이름>`, 기본값은 [agent/config.py](team/agent/config.py))
| 구분 | 설정 | 기본값 |
|---|---|---|
| 말 끝 판단 | `VAD`, `COMPLETE_SILENCE_MS`, `MAX_SILENCE_MS`, `SEMANTIC_EOU`, `STT_TRIGGER_SILENCE_MS` | webrtc, 1100, 1800, 1, 400 |
| 말 끝 판단 모드 | `EOU_MODE`: vad(기존) / soniox_stream(Soniox `<end>`, **실제 키 미검증**) | vad |
| STT | `STT_PROVIDER`(openai·soniox·faster_whisper·fake), `STT_MODEL`, `SONIOX_MODEL` | openai, gpt-4o-mini-transcribe, stt-rt-v4 |
| LLM | `LLM_PROVIDER`(openai_compat·scripted), `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY_ENV` | openai_compat, OpenAI, gpt-4.1, OPENAI_API_KEY |
| TTS | `TTS_PROVIDER`(openai·gemini·sapi·tone), `TTS_VOICE`, `GEMINI_TTS_VOICE`, `GEMINI_TTS_STREAM` | openai, coral, Kore, 0 |
| 함수·출력 검사 | `TOOL_GATE`(실행 전 관문), `OUTPUT_GUARD`(log: 근거 없는 숫자 기록만·반복 문장 버림 / block / off), `PACK_DIR`(공식 규칙 묶음) | 1, log, (없음) |
| 지연 줄이기 | `TOOL_ACK`(조회 중 멘트), `FIRST_CHUNK_MIN_CHARS`(첫 조각 쉼표 끊기) — **기본 꺼짐, 팀 결정 후 켬** | 0, 0 |
| 응대 | `PROMPT_PATH`, `PHRASES_PATH`, `RULES_PATH`, `TURN_BUDGET_S`, `TOOL_TIMEOUT_S` | prompts/practice_*, 20, 8 |
| 출력 | `OUTPUT_PACING`, `SEND_TRANSCRIPT_EVENT`, `LOG_TEXT` | burst, 1, 0 |

API 키 값은 코드와 로그에 들어가지 않습니다. 설정에는 키가 담긴 **환경 변수의 이름**만 적습니다.

## 파일 구성
```
team/
  agent/                  대회용 상담원 서버 (운영진 측정기가 접속하는 곳)
    server.py             WebSocket 진입점, 공급자 초기화, 고정 문구 미리 합성
    session.py            연결 1개 = 세션 1개: 말 끝 판단 → 응답 → turn.end
    endpointing.py        프레임 단위 VAD, 발화 버퍼
    soniox_stream.py      선택 모드: Soniox 스트리밍 말 끝 판단
    dialog.py             LLM ↔ 함수 호출 반복, 문장 스트리밍, 관문·출력 검사 연결
    gate.py               실행 전 관문 (형식·근거·고객 동의)
    output_guard.py       출력 검사 (근거 없는 숫자, 반복 문장)
    tools_client.py       tools_url·X-Session-Id 호출, 정의되지 않은 함수 차단
    policy.py             프롬프트 조립, 발신자·이력, 인계 후보 키워드
    config.py             모든 설정 (환경 변수)
    providers/            stt.py, llm.py, tts.py
    prompts/              연습용 지침·규칙·고정 문장 ← 11/21 공식 자료로 교체 (prompts/README.md 체크리스트)
  devui/                  브라우저 테스트 화면 + 테스트 서버 (개발용, 대회 경로와 분리)
  mock/mock_api.py        모의 함수 서버
  scripts/                bench(반복 측정), check_audio(STT·TTS 단독), check_llm, llm_probe(LLM·관문),
                          check_concurrency / check_external(운영 점검), sweep_endpoint(말 끝 실험), make_testset
  tests/                  pytest
  data/                   practice_ko(연습 8문항), endpoint_ko(말 끝 실험 18개), gate_ko(관문 시험), holdout_ko(튜닝 안 하는 세트)
  docs/                   문서 (아래)
  runs/                   측정 결과
```

## 문서
| 문서 | 내용 |
|---|---|
| [docs/INTEGRATION_REPORT.md](team/docs/INTEGRATION_REPORT.md) | 통합 작업 보고 (무엇을 합쳤고, 무엇을 검증했나) |
| [docs/IMPROVEMENTS.md](team/docs/IMPROVEMENTS.md) | **팀 개선 과제** (말 끝 판단, STT, TTS, LLM, 속도, 사용자 경험) |
| [docs/API_CHECK_1121.md](team/docs/API_CHECK_1121.md) | **11/21 공식 키 검증 절차** |
| [docs/OPS.md](team/docs/OPS.md) | 측정 당일 절차, 외부 접속, 동시 4연결, 제출 압축 |
| [devui/README.md](team/devui/README.md) | 브라우저 테스트 화면 상세 |
| [docs/PROTOTYPE.md](team/docs/PROTOTYPE.md) | 통합 상세, 측정기 호환성 |
| [docs/ISSUES.md](team/docs/ISSUES.md), [docs/LLM_BACKLOG.md](team/docs/LLM_BACKLOG.md) | 문제 사례, LLM 보류 목록 |
| [docs/LLM_GATE.md](team/docs/LLM_GATE.md), [docs/LATENCY.md](team/docs/LATENCY.md), [docs/ENDPOINTING.md](team/docs/ENDPOINTING.md), [docs/SPEAK.md](team/docs/SPEAK.md) | 기능별 설명 |
| [docs/REPORT.md](team/docs/REPORT.md) | 통합 전 1차 측정 보고서 |
