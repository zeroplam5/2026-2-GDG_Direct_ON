# prototype 브랜치: 기능 브랜치 통합과 브라우저 연결

2026-11-20 · 대상: 팀원 · 키 없이 대체물로만 검증함 (실제 API 검증 절차: [API_CHECK_1121.md](API_CHECK_1121.md))

```
브라우저 마이크 ─ 16kHz s16le mono 640B/20ms ─▶ team.devui.server /ws (9100, 127.0.0.1)
                                                    │  같은 Session·Dialog·ToolsClient 코드
운영진 측정기 ────────────────────────────────▶ team.agent.server (9000)  ← 대회 경로, debug 없음
                                                    ▼
          VAD·말 끝 판단 → STT → LLM ─▶ 실행 전 관문 ─▶ tools_url (X-Session-Id) ─▶ 결과 → LLM
                                     └▶ 출력 검사 → TTS → PCM → turn.end
```

## 1. 보존한 것
| 무엇 | 어디 |
|---|---|
| 통합 전 미커밋 작업 전체 (devui 화면·테스트 서버·디버그 훅, README, .gitignore) | 커밋 `d7a8548`, 태그 `backup/devui-wip-20261120` |
| `main` | 그대로 (`343d195`). prototype은 여기서 갈라짐 |
| 원격의 `protortype`(철자 다름) | 건드리지 않음. `main`과 같은 커밋이라 추가 내용 없음 |

되돌리기: `git switch -c restore backup/devui-wip-20261120` 이면 통합 직전 상태입니다.

## 2. 통합 순서와 충돌 해결
| 순서 | 브랜치 | 충돌 | 해결 |
|---|---|---|---|
| 1 | new-models | README | devui 안내와 공식 모델 안내 둘 다 유지 |
| 2 | latency | dialog.py, README | `Ack` 초기화 + devui debug 훅 둘 다 유지, `debug.tool_ack` 추가 |
| 3 | llm-gate | dialog.py | 출력 검사를 거친 문장 + devui `llm.round_end` 둘 다 유지. 막힌 호출이 화면에 안 보이던 문제를 `debug.gate`/`blocked_reply`/`output_guard`로 보완 |
| 4 | measure-ops | 없음 | |
| 5 | endpointing | 없음 | 독립 실험 서버는 본 서버와 연결돼 있지 않아서, 본 Session에 선택 모드로 옮김(아래) |

기본값은 팀이 정한 그대로입니다: 관문 켬, 출력 검사 `log`, 조회 중 멘트 끔, 첫 조각 끊기 끔, 모델은 예전 OpenAI 3종(.env로 공식 모델 선택).

### 출력 검사 `log`의 실제 동작
- 근거 없는 숫자: **기록만** 하고 문장은 그대로 말합니다(차단 아님).
- 같은 턴에서 이미 말한 문장: `log`에서도 **버립니다**(`off`일 때만 그대로).

### 말 끝 판단: Soniox 스트리밍 선택 모드 (`AGENT_EOU_MODE=soniox_stream`, 기본 `vad`)
`team/scripts/endpoint_server.py`는 `team.agent`를 쓰지 않는 실험 서버(답 = 들은 말 따라 하기)입니다. 같은 방식을 본 Session에 옮겨, 전체 파이프라인·측정기로 비교할 수 있게 했습니다.
- 세션마다 Soniox WebSocket 하나에 모든 프레임을 보냅니다. `<end>`가 오고 로컬 무음이 300 ms 이상이면 확정합니다.
- `<end>` 뒤 새 토큰이 오면 이어 말한 것으로 보고 대기를 취소합니다. 텍스트는 확정 때까지 누적합니다.
- `<end>`가 오지 않으면 로컬 무음 3초 뒤 일괄 STT로 처리합니다. 연결 전이나 연결 실패면 기존 `vad` 방식으로 동작합니다.
- **가짜 Soniox 서버로만 시험했습니다.** 기존 방식보다 낫다고 판단할 근거는 아직 없습니다.

## 3. 브라우저 ↔ 상담원 서버 연결
- 화면: `team/devui/static/` (기존 화면 유지, 표시 항목만 추가). 서버: `team/devui/server.py`.
- 테스트마다 새 WebSocket → `session.start`(session_id, now, tools_url, caller, history, audio) → AudioWorklet이 마이크(보통 48 kHz, 1~2ch)를 mono → 16 kHz → s16le → **640 B(20 ms)** 로 보냅니다.
- `turn.end`를 받을 때까지 무음도 계속 보냅니다. 말 끝은 서버가 판단합니다(중지 버튼은 판단을 건너뛰지 않고 연결을 정리함).
- 받은 PCM은 받은 순서대로 이어 재생합니다. "서버 응답 완료(turn.end)"와 "재생 완료"는 따로 표시합니다.
- 디버그 정보(인식 텍스트, LLM 라운드, 관문 판정, 함수 인자·결과, 시간)는 `/ws` 경로에서만 나갑니다. 대회 서버는 `debug=None`이라 보내지 않습니다.
- 호환 모드: 화면에서 URL을 `ws://127.0.0.1:9000`(대회 서버)으로 바꾸면 측정기와 같은 메시지만 받습니다.
- `history`: 공식 형식은 미공개입니다. 화면은 `[{"role":"user"|"assistant","text":…}]`로 넣고, 서버는 `policy.normalize_history`에서 role/speaker, text/content/utterance를 읽습니다.
- 상담원 연결(인계)은 실제로 하지 않습니다. 화면도 "연결 완료"를 표시하지 않습니다.

## 4. 실제로 실행한 검증 (2026-11-20, 이 PC, 대체물)
| 검증 | 결과 |
|---|---|
| 통합 전 기준선 | pytest 42, Node 12 통과 |
| 각 통합 단계 pytest | 47 → 53 → 83 → 89 → 89 통과 |
| 최종 pytest | 95 통과 + 3 xfail(알려진 한계) |
| Node PCM 변환 테스트 | 12 통과 (리샘플, 리틀엔디언, 640 B 프레임) |
| 관문 경계 사례 `test_gate_edges.py` | 확인 5, 알려진 한계 3 (xfail) |
| Soniox 스트리밍 모드 (가짜 서버) | 5 통과: `<end>` 확정, 이어 말함, 대체 무음, 연결 실패 대체, 기본값 vad |
| 헤드리스 Chrome + 가짜 마이크(K01 wav) → devui → 대체물 파이프라인 | 48 kHz 2ch → 16 kHz mono 640 B 355프레임, 크기 이상 0, 전송 비율 0.999. 말 끝 `complete_text`, 함수 호출이 함수 서버 기록(세션 ID)과 일치, 재생 완료, 마이크·AudioContext 정리 |
| 헤드리스 Chrome + 가짜 이벤트 서버 | 관문 차단 카드, 차단 뒤 응답 방식, 출력 검사 표시가 보임 |
| 스타터킷 측정기 원본으로 동시 1 vs 4 (`check_concurrency`) | 24회씩, 동시 1·4 모두 끼어듦 0 · 오류 0 · turn.end 누락 0 · 답 내용 불일치 0, 첫 소리 p50 1.92 s / 1.93 s (대체물이라 속도 수치 아님. 같은 서버가 4세션을 섞지 않고 처리한다는 확인) |
| 대회 경로에 debug 이벤트 없음 | `test_competition_path_sends_no_debug_events` 통과 |

## 5. 사람이 직접 확인해야 하는 것
- **실제 마이크로 말하기**(위 Chrome 시험은 가짜 마이크 장치였음), 스피커 재생 품질·끊김, 에코.
- Safari·Firefox, 장치별 샘플레이트(44.1 kHz 블루투스 등).
- 실제 API 전체(Soniox·Gemini·gpt-5.4-mini), 실제 API 4개 동시, **다른 기기**에서의 접속 → [API_CHECK_1121.md](API_CHECK_1121.md).

## 6. 측정기 호환성
- 대회 경로의 메시지는 그대로입니다(PCM, `agent.transcript`, `turn.end`). 새 이벤트는 devui `/ws`에서만 나갑니다.
- 동작이 바뀌는 부분: **관문(기본 켬)이 동의 없는 접수·변경 호출을 막습니다.** 그래서 함수 서버로 가는 호출과 응답 문장이 통합 전과 달라질 수 있습니다. 끄려면 `AGENT_TOOL_GATE=0`.
- 출력 검사 `log`는 같은 턴에서 반복된 문장을 버립니다.
- metrics 파일(`agent_metrics.jsonl`)에 `eou_mode`, `stt_source`, `soniox_end_ms_rel` 열이 추가됐습니다(측정기와 무관한 팀 기록).

## 7. 알려진 위험과 남은 일
| # | 내용 | 근거 |
|---|---|---|
| 1 | 실제 키 검증 없음: Soniox 인증 방식(config `api_key` vs `Bearer` 헤더가 두 브랜치에서 다름), Gemini 24 kHz 가정, gpt-5.4-mini `reasoning_effort=none` | 코드 비교 |
| 2 | 관문: 같은 값이면 **다른 업무**도 통과(콜백 질문에 "네" → 연락처 변경 함수도 통과) | `test_gate_edges.py` xfail |
| 3 | 관문: 조회 함수는 **정정 전 값**으로 불러도 통과 ("데모 일공공일, 아니 데모 일공공이" → DEMO1001 조회 허용) | 같음 |
| 4 | 관문: 정정 때 "데모"를 생략하면 새 값이 근거 없음으로 막힘 ("아니 일공공이요" → DEMO1002 차단, 다시 물음) | 같음 |
| 5 | "일공공일**이**에요"의 '이'가 숫자 2로 붙음 (Chrome 시험에서 DEMO10012로 조회, not_found). 대체물 LLM에서 본 것이지만 관문의 숫자 정규화도 같은 방식 | Chrome E2E, ISSUES #3, LLM_BACKLOG G2 |
| 6 | 동시 4는 대체물로만 확인. API 지연·속도 제한은 미확인 | OPS.md |
| 7 | 공식 history 형식, 함수 목록, 매뉴얼 미반영 (11/21) | prompts/README.md |
| 8 | devui는 한 연결에 고객 발화 1회, 끼어들기 없음(서버도 응답 중 입력 무시) | devui/README.md |
| 9 | `AGENT_TOOL_ACK`·첫 조각 끊기 효과는 가정한 TTS 지연으로만 측정 | LATENCY.md |

## 8. 로컬 실행
```bash
# 자동 테스트 (키 없음)
.venv/Scripts/python -m pytest team/tests -q
node --test team/devui/tests/pcm.test.mjs

# 브라우저 테스트 (키 없이 흐름만: 대체물)
.venv/Scripts/python -m team.mock.mock_api --port 8100
.venv/Scripts/python -m team.devui.server --stt fake --llm scripted --tts tone
#   → Chrome/Edge에서 http://127.0.0.1:9100 → 테스트 시작 → 마이크 허용 → 한 번 말하고 기다림
#   (--stt fake는 소리와 상관없이 AGENT_STT_FAKE_TEXT를 돌려줌. 예: AGENT_STT_FAKE_TEXT="주문번호 데모 일공공일 배송 어디쯤이에요?")

# 키가 있을 때 (.env의 AGENT_* 설정 그대로)
.venv/Scripts/python -m team.devui.server

# 대회용 서버 (운영진 측정기가 붙는 곳)
.venv/Scripts/python -m team.agent.server --port 9000
```
화면에서 볼 것: 진행 상태 → "고객 음성 수신 중" → "발화 종료 확정" → "서버 응답 완료(turn.end)", 재생 상태 → "재생 완료". "내가 한 말"(서버 STT), 함수 호출 카드(관문 차단 포함), 시간 표.
