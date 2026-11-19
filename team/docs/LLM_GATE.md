# LLM·함수: 실행 전 관문과 규칙 묶음 (팀원용 안내)

브랜치 `feature/llm-gate` · 담당 유주연 · 2026-11-19

> **`feature/latency` 기준으로 작업했다.** 이 브랜치는 `feature/latency`의 최신 커밋(`8ddb17a`, 조회 중 멘트·첫 조각 끊기 기본값 꺼짐)에서 시작했고, 그 위에 LLM·함수 커밋을 얹었다.
> 그래서 `main`과 비교하면 `feature/latency`의 커밋 3개까지 함께 보인다. 합칠 때는 `feature/latency`가 먼저 `main`에 들어가거나, PR 기준 브랜치를 `feature/latency`로 잡는다.
> 스타터킷 원본 파일은 고치지 않았다.

## 한 줄 요약

LLM이 함수를 부르기 전에 **코드가 한 번 더 검사**하게 했다. 고객이 동의하지 않은 접수·변경은 실제로 보내지지 않는다. 그리고 그 규칙을 **파일 하나**에 모아서, 11/21 공식 매뉴얼이 나오면 그 파일만 채우면 되게 했다.

## 왜 필요했나

- 팀 시험(K07)에서 고객이 "내일 다시 전화 주실 수 있나요?"라고만 했는데, 모델이 **확인 없이 콜백을 접수**했다. gpt-5는 3/3, 이번 지정 모델 gpt-5.4-mini도 3/3이었다.
- 프롬프트에 "번호를 쓰기 전에 고객에게 확인하라"는 지시가 있었는데도 그랬다. 프롬프트만으로는 막히지 않는다.
- 측정 때는 운영진 중계가 함수 호출을 전부 기록하고, "호출하면 안 될 때 호출해도 틀림"이다. 한 번 보낸 호출은 되돌릴 수 없다.

## 무엇이 바뀌었나

```
고객 발화 ─▶ LLM ─┬─ 말 ─────────────────────────────▶ [출력 검사] ─▶ TTS
                  └─ 함수 호출 ─▶ [실행 전 관문] ─┬─ 통과 ─▶ tools_url로 전송 ─▶ 결과 ─▶ LLM
                                                  └─ 차단 ─▶ 전송 안 함 ─▶ 확인 질문
```

| 바뀐 것 | 내용 | 파일 |
|---|---|---|
| 실행 전 관문 | 함수를 보내기 전에 형식, 값이 대화에 실제로 나왔는지, 접수·변경이면 고객 동의(무엇을·어떤 값으로·"네")를 검사 | `team/agent/gate.py` |
| 규칙 한 곳으로 | 함수별 규칙, 인계 키워드, 동의·거절 단어를 한 파일에. 프롬프트의 [함수 실행 규칙]은 이 표에서 자동 생성 | `team/agent/prompts/practice_rules.json` |
| 막혔을 때의 말 | 이미 확인 질문을 했으면 다시 말하지 않음. 정해진 확인 문장이 있으면 모델을 다시 부르지 않고 바로 말함 | `team/agent/dialog.py` |
| 출력 검사 | TTS 전에 문장마다 검사: 근거 없는 숫자는 기록, 한 턴에 이미 말한 문장은 버림 | `team/agent/output_guard.py` |
| 프롬프트 배치 | 매 통화 같은 지침을 앞에, 시각·발신자·이력을 끝에 (캐시에 유리) | `team/agent/policy.py` |
| 확인 도구 | 터미널에서 LLM 결정, 관문 판정, 결과 뒤 답까지 표로 보여 줌 | `team/scripts/llm_probe.py` |

## 팀원에게 미치는 영향

- **기본값이 바뀐다.** 관문은 켜져 있다. 출력 검사는 기록만 하고(`log`), 막지는 않는다.
  - 끄기: `AGENT_TOOL_GATE=0`
  - 출력 검사 끄기: `AGENT_OUTPUT_GUARD=off`
- **연습 지침에서 `{now}`를 뺐다.** 현재 시각은 시스템 프롬프트 끝의 [상담 정보]로 자동으로 붙는다. 지침 파일을 고칠 때 `{now}`를 다시 넣지 않는 게 좋다(넣어도 동작은 함).
- **음성 쪽 코드(STT·TTS·말끝 판정)는 건드리지 않았다.** `session.py`는 대화 객체를 만드는 몇 줄만 바뀌었다.
- **다른 브랜치와 겹치는 파일이 없다.** `feature/measure-ops`가 고친 파일과 이 커밋의 파일은 하나도 겹치지 않는다(2026-11-19 확인).

## 직접 돌려 보기

```
.venv/Scripts/python -m pytest team/tests -q                                  # 80개
.venv/Scripts/python -m team.scripts.llm_probe --case K07                     # 문항 하나
.venv/Scripts/python -m team.scripts.llm_probe --suite gate --full            # 시험 문항 9개, 결과 뒤 답까지
.venv/Scripts/python -m team.scripts.llm_probe --case K07 --show-prompt       # 실제로 들어가는 프롬프트 보기
```

LLM 키는 `.env`에 둔다. OpenRouter 키로 시험할 때 필요한 설정은 아래와 같다(모델 이름이 `openai/`로 시작해 추론 모델 설정을 직접 넣어야 함).

```
OPENROUTER_API_KEY=...
AGENT_LLM_BASE_URL=https://openrouter.ai/api/v1
AGENT_LLM_API_KEY_ENV=OPENROUTER_API_KEY
AGENT_LLM_MODEL=openai/gpt-5.4-mini
AGENT_LLM_TEMPERATURE=-1
AGENT_LLM_TOKEN_PARAM=max_completion_tokens
AGENT_LLM_MAX_TOKENS=1024
AGENT_LLM_REASONING_EFFORT=none
```

## 측정 결과 (OpenRouter 경유 gpt-5.4-mini, 기능 확인용)

| 시험 | 결과 |
|---|---|
| 관문 시험 문항 9개 × 3회 | 27/27 기대와 일치 |
| 모델이 잘못 부르려던 호출 | 규칙 통합 전 9회 → 통합 후 2회 (남은 2회는 관문이 막음) |
| 결과 뒤 답까지 (`--full`) | 9/9 기대와 일치 |

시간 수치는 OpenRouter를 거친 값이라 OpenAI 직결과 다르다. 공식 키로 다시 잰다.

## 11/21에 할 일

`team/agent/prompts/README.md`의 체크리스트 8단계를 따른다. 요점은 다음과 같다.

1. 공식 함수 목록을 규칙 파일에 채운다. 표에 없는 함수는 안전하게 "확인 필요"로 막힌다.
2. 매뉴얼은 지침 파일로 옮긴다.
3. 공식 history 형식에 맞춰 `policy.normalize_history` 한 곳만 고친다.

## 아직 안 한 것

- 세부 개선 27개(응답 품질 11, 지연 4, 관문 정확도 6, 기능 6)를 `team/docs/LLM_BACKLOG.md`에 모아 두었다. 예: 번호 읽기 형식 통일, 결과 뒤 답에서 인자에 없는 조건("내일") 말하기, 모델이 답을 두 번 쓰는 원인.
- 인계(사람 연결)는 지금도 메모만 붙이고, 판단은 모델이 한다. 매뉴얼을 보고 정한다.
