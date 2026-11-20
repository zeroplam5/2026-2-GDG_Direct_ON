# prototype 통합 작업 보고 (2026-11-20)

대상: 팀원 · 브랜치 `prototype` · 원격에는 아직 올리지 않음

## 한눈에 보기

- 기능 브랜치 5개를 `prototype`에 합쳤습니다: new-models, latency, llm-gate, measure-ops, endpointing.
- 브라우저 테스트 화면(devui)을 상담원 서버의 실제 처리 경로에 연결했습니다.
- **모든 기능은 아직 실제 API 키로 검증하지 않았습니다.** 지금까지는 키 없이 대체물(가짜 STT, 스크립트 LLM, 신호음 TTS, 가짜 Soniox 서버)로만 확인했습니다.
- 최종 자동 테스트: pytest 95개 통과 + 3개 xfail(알려진 한계 표시), Node 12개 통과.
- 11/21 공식 키를 받은 뒤 할 일은 [API_CHECK_1121.md](API_CHECK_1121.md)에 순서대로 정리했습니다.

```
브라우저 마이크 ─ 16kHz s16le mono, 640B/20ms ─▶ team.devui.server /ws (9100, 127.0.0.1만)
                                                     │  대회 서버와 같은 Session·Dialog·ToolsClient 코드
운영진 측정기 ─────────────────────────────────▶ team.agent.server (9000)  ← 대회 경로, 디버그 이벤트 없음
                                                     ▼
       VAD·말 끝 판단 → STT → LLM ─▶ 실행 전 관문 ─▶ tools_url (X-Session-Id) ─▶ 결과 → LLM
                                  └▶ 출력 검사 → TTS → PCM → turn.end
```

## 1. 기존 작업 보존

| 무엇 | 어디 |
|---|---|
| 통합 전 커밋하지 않았던 작업 전체 (devui 화면·테스트 서버·디버그 훅, README, .gitignore) | 커밋 `d7a8548`, 태그 `backup/devui-wip-20261120` |
| `main` | 그대로 (`343d195`). `prototype`은 여기서 갈라짐 |
| 원격의 `protortype` (철자 다름) | 건드리지 않음. `main`과 같은 커밋이라 추가 내용 없음 |

- 커밋 전에 패치와 미추적 파일 압축본도 따로 만들었습니다(작업 PC의 임시 폴더).
- reset, 강제 체크아웃, 파일 삭제는 쓰지 않았습니다.
- 통합 직전 상태로 돌아가려면: `git switch -c restore backup/devui-wip-20261120`

## 2. 통합 순서와 충돌 해결

| 순서 | 브랜치 | 충돌 | 해결 |
|---|---|---|---|
| 1 | new-models | README | devui 안내와 공식 모델 안내 둘 다 유지 |
| 2 | latency | dialog.py, README | 조회 중 멘트 초기화와 devui 디버그 훅 둘 다 유지 |
| 3 | llm-gate | dialog.py | 출력 검사를 거친 문장과 devui 디버그 이벤트 둘 다 유지 |
| 4 | measure-ops | 없음 | |
| 5 | endpointing | 없음 | |

통합 단계마다 테스트했습니다: 기준선 42 → 47 → 53 → 83 → 89 → 89 통과.

기본값은 팀이 정한 그대로입니다.

| 설정 | 기본값 | 의미 |
|---|---|---|
| `AGENT_TOOL_GATE` | 1 (켬) | 실행 전 관문 |
| `AGENT_OUTPUT_GUARD` | log | 근거 없는 숫자는 기록만 함. 같은 턴 반복 문장은 버림 |
| `AGENT_TOOL_ACK` | 0 (끔) | 조회 중 멘트 |
| `AGENT_FIRST_CHUNK_MIN_CHARS` | 0 (끔) | 첫 조각 쉼표에서 끊기 |
| `AGENT_EOU_MODE` | vad | 기존 말 끝 판단 방식 |
| 모델 | 예전 OpenAI 3종 | 공식 모델은 `.env`에서 선택 |

## 3. 코드에서 확인한 사실 (팀 설명과 다르거나 덧붙일 점)

- **Soniox STT는 일괄 방식입니다.** 호출마다 WebSocket을 새로 열고 발화 전체를 한꺼번에 보냅니다. 발화 중 계속 인식하는 방식이 아닙니다.
- **두 브랜치의 Soniox 인증 방식이 다릅니다.** new-models는 첫 설정 메시지에 `api_key`를 넣고, endpointing 실험 서버는 `Authorization: Bearer` 헤더를 씁니다. 실제 키로 어느 쪽이 맞는지 확인해야 합니다.
- **endpointing의 독립 서버는 본 상담원과 연결돼 있지 않았습니다.** `team.agent`를 쓰지 않고, 들은 말을 따라 말할 뿐입니다(LLM·함수 없음).
- **출력 검사 `log`는 기록만 하는 게 아닙니다.** 근거 없는 숫자는 기록만 하지만, 같은 턴에서 이미 말한 문장은 실제로 버립니다.

## 4. 통합하면서 추가·수정한 것

| 커밋 | 내용 |
|---|---|
| `17974b1` | Soniox 스트리밍 말 끝 판단을 본 상담원에 **선택 모드**로 연결 (`AGENT_EOU_MODE=soniox_stream`, 기본은 `vad`) |
| `900ec37` | devui 화면에 관문 차단, 차단 뒤 응답 방식, 출력 검사, 조회 중 멘트, Soniox 말 끝 신호 표시 |
| `539f16e` | 문서([PROTOTYPE.md](PROTOTYPE.md), [API_CHECK_1121.md](API_CHECK_1121.md)), STT·TTS 단독 점검 도구 `check_audio.py`, `env.example` 스위치 안내 |
| `bcf9269` | `--stt fake`가 마이크를 듣지 않는다는 경고를 화면과 로그에 추가, 키 없는 실행 예를 `faster_whisper`로 변경 |

### Soniox 스트리밍 선택 모드 동작
- 세션마다 Soniox WebSocket 하나에 모든 20ms 프레임을 보냅니다.
- Soniox가 `<end>`를 보내고 로컬 무음이 300ms 이상이면 발화 종료를 확정합니다.
- `<end>` 뒤에 새 토큰이 오면 이어 말한 것으로 보고 대기를 취소합니다. 텍스트는 확정 때까지 누적하므로, 쉬었다 이어 말해도 앞부분이 남습니다.
- `<end>`가 오지 않으면 로컬 무음 3초 뒤 일괄 STT로 처리합니다. 연결 전이나 연결 실패 때는 기존 `vad` 방식으로 동작합니다.
- **가짜 Soniox 서버로만 시험했습니다.** 기존 방식보다 낫다고 할 근거는 아직 없습니다.

### devui 화면 보완
관문에 막힌 호출은 함수 서버로 가지 않아서, 기존 함수 호출 카드에 나타나지 않았습니다. 다음 디버그 이벤트를 추가해 보이게 했습니다. 모두 **devui `/ws` 경로에서만** 나갑니다.

| 이벤트 | 내용 |
|---|---|
| `debug.gate` | 관문 판정: 함수 이름, 허용 여부, 차단 사유, 인자, 모델에게 돌려준 안내 |
| `debug.blocked_reply` | 막힌 뒤 응답 방식: 이미 질문함 / 규칙 파일의 확인 문장 / 모델이 답함 |
| `debug.output_guard` | 근거 없는 숫자 또는 반복 문장 |
| `debug.tool_ack` | 조회 중 멘트 재생 |
| `debug.eou_mode`, `debug.soniox` | 말 끝 판단 모드, Soniox `<end>` 수신과 이어 말함 |

## 5. 브라우저와 상담원 서버 연결

- 기존 화면(`team/devui/static/`)은 유지하고 표시 항목만 추가했습니다.
- 테스트마다 새 WebSocket을 열고 `session.start`(session_id, now, tools_url, caller, history, audio)를 보냅니다.
- AudioWorklet이 마이크 입력(보통 48kHz, 1~2채널)을 mono 16kHz s16le로 바꿔 **640바이트(20ms)** 단위로 보냅니다.
- `turn.end`를 받을 때까지 무음도 계속 보냅니다. 발화 종료는 서버가 판단합니다.
- 받은 PCM은 받은 순서대로 이어 재생합니다. "서버 응답 완료(turn.end)"와 "재생 완료"를 따로 표시합니다.
- 호환 모드: 화면에서 URL을 `ws://127.0.0.1:9000`(대회 서버)으로 바꾸면 측정기와 같은 메시지만 받습니다.
- `history`: 공식 형식은 아직 공개되지 않았습니다. 화면은 `[{"role":"user"|"assistant","text":…}]`로 넣고, 서버는 `policy.normalize_history`에서 role/speaker, text/content/utterance를 읽습니다.
- 상담원 인계는 실제로 하지 않으며, 화면에 "연결 완료"를 표시하지 않습니다.

## 6. 실행한 검증 (대체물)

| 검증 | 결과 |
|---|---|
| 최종 pytest | 95 통과 + 3 xfail |
| Node PCM 변환 테스트 | 12 통과 (리샘플, 리틀엔디언, 640B 프레임) |
| 관문 경계 사례 `test_gate_edges.py` | 확인 5, 알려진 한계 3 (xfail) |
| Soniox 스트리밍 모드 (가짜 서버) | 5 통과: `<end>` 확정, 이어 말함, 대체 무음, 연결 실패 대체, 기본값 vad |
| 헤드리스 Chrome + 가짜 마이크(K01.wav) → devui → 대체물 파이프라인 | 48kHz 2ch → 16kHz mono 640B 355프레임, 크기 이상 0, 전송 비율 0.999. 말 끝 판단 → 함수 호출(모의 서버 기록과 세션 ID 일치) → 재생 → 마이크·AudioContext 정리 |
| 헤드리스 Chrome + 가짜 이벤트 서버 | 관문 차단 카드, 차단 뒤 응답 방식, 출력 검사 표시가 보임 |
| 스타터킷 측정기 원본, 동시 1 vs 4 (각 24회) | 끼어들기 0, 오류 0, `turn.end` 누락 0, 답 내용 불일치 0 |
| 대회 경로에 디버그 이벤트 없음 | `test_competition_path_sends_no_debug_events` 통과 |

대체물로 잰 시간 값은 속도 수치가 아닙니다. 기능 동작과 세션 혼선 여부를 확인한 것입니다.

### 실제 마이크 시험에서 나온 문제 (해결)
`--stt fake`로 띄우고 마이크로 말했더니 "잘 듣지 못했습니다"만 나왔습니다. fake STT는 소리를 듣지 않고 `AGENT_STT_FAKE_TEXT`를 돌려주는데, 그 값이 비어 있었기 때문입니다. 마이크와 음성 감지는 정상이었습니다. 이제 화면과 로그에 경고가 나오고, 키 없는 실행 예는 로컬 Whisper(`--stt faster_whisper`)로 바꿨습니다.

## 7. 아직 확인하지 못한 것

- 실제 API: Soniox, Gemini TTS, gpt-5.4-mini, 실제 API 4개 동시 세션
- Soniox 스트리밍 모드의 실제 효과
- 사람이 직접 확인할 것: 실제 마이크·스피커 품질과 에코, Safari·Firefox, 장치별 샘플레이트, **다른 기기에서 두 서버 접속**

## 8. 측정기 호환성

- 대회 경로의 메시지 형식은 그대로입니다: PCM, `agent.transcript`, `turn.end`.
- 동작은 달라집니다.
  - 관문이 기본으로 켜져 있어, 동의 없는 접수·변경 호출을 막습니다. 끄려면 `AGENT_TOOL_GATE=0`.
  - 출력 검사가 같은 턴에서 반복된 문장을 버립니다.
  - 팀 기록 파일 `agent_metrics.jsonl`에 `eou_mode`, `stt_source`, `soniox_end_ms_rel` 열이 추가됐습니다.

## 9. 알려진 위험과 남은 일

| # | 내용 |
|---|---|
| 1 | 실제 키 검증 없음: Soniox 인증 방식, Gemini 24kHz 가정, gpt-5.4-mini `reasoning_effort=none` |
| 2 | 관문: 값이 같으면 **다른 업무**도 통과 (콜백 질문에 "네" → 연락처 변경 함수도 통과) |
| 3 | 관문: 조회 함수는 **정정 전 값**으로 불러도 통과 |
| 4 | 관문: 정정 때 "데모"를 빼면 새 값이 "대화에 없는 값"으로 막힘 |
| 5 | "일공공일**이**에요"의 '이'가 숫자로 붙음 (Chrome 시험에서 `DEMO10012`로 조회). 관문의 숫자 정규화도 같은 방식 |
| 6 | 동시 4는 대체물로만 확인. API 지연·속도 제한은 미확인 |
| 7 | 공식 history 형식, 함수 목록, 응대 매뉴얼 미반영 (11/21) |
| 8 | devui는 한 연결에 고객 발화 1회, 끼어들기 없음 |
| 9 | 조회 중 멘트·첫 조각 끊기 효과는 가정한 TTS 지연으로만 측정 |

2~4번은 관문 담당자가 정할 일이라 로직은 고치지 않고 xfail 테스트로 남겼습니다.

## 10. 실행 명령

```bash
# 자동 테스트 (키 없음)
.venv/Scripts/python -m pytest team/tests -q
node --test team/devui/tests/pcm.test.mjs team/devui/tests/score.test.mjs

# 모의 함수 서버 (모든 브라우저 테스트에 필요)
.venv/Scripts/python -m team.mock.mock_api --port 8100

# 키 없이: 로컬 Whisper로 실제 인식 + 대체물 답(scripted)과 신호음(tone)
.venv/Scripts/python -m team.devui.server --stt faster_whisper --llm scripted --tts tone

# 실제 AI 답변까지: OpenAI 키 사용(비용 발생), 음성은 Windows 한국어 음성
.venv/Scripts/python -m team.devui.server --stt faster_whisper --llm openai_compat --tts sapi
#   공식 LLM으로: --llm-model gpt-5.4-mini 추가 (아직 실제로 호출해 본 적 없음)

# 대회용 서버 (운영진 측정기가 붙는 곳)
.venv/Scripts/python -m team.agent.server --port 9000
```

브라우저: Chrome이나 Edge에서 http://127.0.0.1:9100 → 테스트 시작 → 마이크 허용 → 한 번 말하고 기다리기.

## 11. 11/21 공식 키·자료를 받은 뒤

[API_CHECK_1121.md](API_CHECK_1121.md)를 순서대로 따릅니다. 비용을 아끼려고 짧은 음성 1개, 요청 1회부터 시작합니다.

1. Soniox STT 단독 호출 (`check_audio --stt`)
2. Gemini TTS 단독 호출과 재생 (`check_audio --tts --play`)
3. gpt-5.4-mini 일반 응답과 함수 호출 (`check_llm`, `llm_probe`)
4. 실제 API 전체 세션 1회 (`--limit 1 --concurrency 1`)
5. 긴 쉼, 숫자 정정, 함수 실패, 동의 없는 요청
6. 실제 API 4개 동시 세션
7. 다른 기기에서 두 서버 주소 접속
8. 공식 매뉴얼·함수 정의·모의 데이터 반영 후 재측정

## 관련 문서

- [PROTOTYPE.md](PROTOTYPE.md): 통합 상세
- [API_CHECK_1121.md](API_CHECK_1121.md): 11/21 실제 API 검증 절차
- [../devui/README.md](../devui/README.md): 브라우저 테스트 화면
- [LLM_GATE.md](LLM_GATE.md), [LATENCY.md](LATENCY.md), [OPS.md](OPS.md), [ENDPOINTING.md](ENDPOINTING.md): 브랜치별 원래 문서
