# 첫 소리 줄이기: 조회 중 멘트 · 첫 조각 빨리 끊기

2026-11-19 · 브랜치 `feature/latency` · 연습용 측정이며 공식 점수가 아닙니다.

> **두 기능 모두 기본값은 꺼져 있습니다.** 운영진·팀이 정한 기능이 아니라 제안이기 때문입니다. 켜려면 `AGENT_TOOL_ACK=1`, `AGENT_FIRST_CHUNK_MIN_CHARS=8`.

## 왜
팀 1차 측정(REPORT.md)에서 첫 소리는 p50 3.34초였고, 단계별로 발화 종료 판단 1.15초 · LLM 1.15초 · 첫 문장 TTS 0.93초였습니다.
함수를 부르는 문항은 LLM을 두 번 부르므로 더 느립니다. 데모 점수표는 함수 문항의 속도 점수를 **(첫 소리 점수 + 조회 뒤 답 점수) ÷ 2**로 매깁니다.
11/21 공식 TTS(Gemini)는 문장을 다 만든 뒤 소리를 주는 방식이 기본이라, 문장이 길수록 첫 소리가 늦어질 수 있습니다.

## 무엇을 바꿨나
| 기능 | 동작 | 설정 (기본값) |
|---|---|---|
| 조회 중 멘트 | 모델이 **아무 말 없이 함수부터 부르면**, 함수를 실행하는 동안 미리 합성해 둔 짧은 멘트를 바로 보냅니다. 3개 문구를 돌아가며 써서 같은 말을 반복하지 않습니다. 모델이 먼저 말을 했으면 멘트를 넣지 않습니다. | `AGENT_TOOL_ACK` (기본 0), 문구는 `practice_phrases.json`의 `tool_ack` |
| 첫 조각 빨리 끊기 | 턴의 첫 조각만, 마침표 전이라도 8자 이상 뒤의 쉼표에서 끊어 TTS를 먼저 시작합니다. 두 번째 조각부터는 문장 단위 그대로입니다. | `AGENT_FIRST_CHUNK_MIN_CHARS` (기본 0 = 끔, 측정 때는 8) |

- 조회 중 멘트는 "답"으로 치지 않습니다. 그래서 시간 초과·오류 때의 안내 멘트("확인이 지연되고 있어…")는 멘트 뒤에도 그대로 나갑니다.
- 문구는 공식 매뉴얼의 말투 규칙이 나오면 바꿔야 합니다. 매뉴얼이 채움말을 막으면 `AGENT_TOOL_ACK=0`.

## 측정
- 음성: 팀 연습 음성 8문항(`team/data/practice_ko`) × 2회, 스타터킷 측정기 그대로
- STT: 가짜(문항별 문장 매핑), LLM: 스크립트 대체물(지연 600ms), TTS: 신호음 대체물
- **TTS 지연은 가정입니다:** 300ms + 글자당 15ms. 문장을 다 만든 뒤 소리를 주는 TTS를 흉내 낸 값이고, 실제 Gemini 수치가 아닙니다.
- 원자료: `team/runs/latency_sim_ack_off/`, `team/runs/latency_sim_ack_on/`

| 문항 | 함수 | 첫 소리 (끔 → 켬) | 조회 뒤 답 (끔 → 켬) | 데모 속도 점수 (끔 → 켬) |
|---|---|---|---|---|
| K01 | 1 | 2.95 → **1.68** s | 1.26 → 1.26 s | 85 → **95** |
| K02 | 1 | 2.93 → **1.66** s | 1.26 → 1.26 s | 85 → **95** |
| K03 | 1 | 3.22 → **1.68** s | 1.53 → 1.53 s | 75 → **90** |
| K06 | 0 | 2.33 → 2.33 s | - | 80 → 80 |
| K07 | 0 | 2.37 → 2.37 s | - | 80 → 80 |
| K08 | 1 | 3.22 → **1.68** s | 1.53 → 1.53 s | 75 → **90** |
| K04, K05 | 1 | 끼어들기 (양쪽 같음) | | 0 → 0 |
| **평균** | | 함수 문항 p50 3.1 → 1.7 s | 변화 없음 | **60.0 → 66.2** |

- 함수 문항의 첫 소리가 약 1.3~1.5초 빨라졌고, 조회 뒤 답 시간은 그대로입니다(멘트를 보내는 동안 함수가 같이 돌기 때문).
- 함수가 없는 문항(K06, K07)은 변화가 없습니다.
- K04·K05 끼어들기는 이번 변경과 무관합니다. 가짜 STT가 소리와 상관없이 완성된 문장을 바로 돌려줘서, 1.3초 쉼 중간에 "말 끝"으로 판단한 결과입니다(실제 STT에서는 REPORT.md 기준 K05만 2/24).
- **첫 조각 빨리 끊기는 이 측정에 나타나지 않습니다.** 스크립트 LLM의 답에 쉼표가 없기 때문입니다. 실제 LLM + Gemini TTS로 재야 합니다.

## 실제 모델로 다시 잴 것
```bash
.venv/Scripts/python -m team.mock.mock_api --port 8100 &
.venv/Scripts/python -m team.scripts.bench --llm openai_compat --configs default_1100_1800 --repeats 3 --out team/runs/real_ack_on --agent-env "AGENT_TOOL_ACK=1,AGENT_FIRST_CHUNK_MIN_CHARS=8"
.venv/Scripts/python -m team.scripts.bench --llm openai_compat --configs default_1100_1800 --repeats 3 --out team/runs/real_ack_off --agent-env "AGENT_TOOL_ACK=0,AGENT_FIRST_CHUNK_MIN_CHARS=0"
```
공식 모델을 쓸 때는 `--agent-env`에 `AGENT_STT_PROVIDER=soniox,AGENT_TTS_PROVIDER=gemini,AGENT_LLM_MODEL=gpt-5.4-mini`를 더합니다(`feature/new-models` 브랜치 필요).
