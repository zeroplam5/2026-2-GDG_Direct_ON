# 규칙 묶음 (pack)

상담원이 읽는 브랜드별 내용은 모두 "묶음" 폴더 하나에 있다. 이 폴더(`team/agent/prompts/`)는 **연습용 묶음**이다.

| 파일 | 담는 것 | 누가 읽나 |
|---|---|---|
| `pack.json` | 이 묶음의 파일 목록 | `config.apply_pack` |
| `practice_system_prompt.md` | 응대 지침·말투 (사람 말로 쓴 글만) | 시스템 프롬프트 |
| `practice_rules.json` | 코드가 판단하는 규칙 전부: 함수별 실행 규칙, 인계 신호 키워드, 동의·거절 단어 | 실행 전 관문, 인계 메모, 프롬프트의 [함수 실행 규칙] 자동 생성 |
| `practice_phrases.json` | 고정 문장 (못 알아들음, 지연, 오류 안내 등) | 서버가 미리 합성 |

원칙: **같은 규칙을 두 곳에 쓰지 않는다.** 함수를 언제 불러도 되는지는 `rules.json`에만 쓰고, 프롬프트에는 그 표에서 자동으로 만든 문장이 붙는다. 그래서 모델이 듣는 지시와 관문이 막는 기준이 항상 같다.

## 묶음 바꾸기

```
AGENT_PACK_DIR=team/agent/packs/official     # pack.json이 있는 폴더
```

`AGENT_PROMPT_PATH`, `AGENT_RULES_PATH`, `AGENT_PHRASES_PATH`를 따로 주면 그 값이 묶음보다 우선한다.

## 11/21 적용 체크리스트

1. **공식 묶음 폴더 만들기**: `team/agent/packs/official/`에 이 폴더의 네 파일을 복사하고 이름을 바꾼 뒤 `pack.json`을 고친다. 공식 자료는 저장소에 공개로 올리지 않는다.
2. **지침 옮기기** (`*_system_prompt.md`): 응대 매뉴얼·브랜드 정보·말투 규칙을 사람 말로 옮긴다. "어떤 함수를 언제 부른다"는 문장은 여기 쓰지 않는다.
3. **함수 규칙 채우기** (`*_rules.json`의 `functions`): 공식 함수마다 한 줄씩 적는다.
   - `kind`: 조회(`read`) 또는 접수·변경(`write`)
   - `confirm` (접수·변경만): `explicit` = 직전에 값을 복창해 묻고 고객이 동의 / `request` = 고객이 값을 직접 말하며 요청하면 충분 / `implicit` = 다시 묻지 않음. 값이 대화에 있고 거절·머뭇거림만 없으면 통과 (공식 묶음 `team/agent/packs/official`의 접수 함수 전부) / `none` = 확인 없음
   - `value_args`: 대화에 실제로 나온 값이어야 하는 인자
   - `formats`: 인자 형식 (정규식, 공백·하이픈 제거 뒤 비교)
   - `arg_labels`: 인자의 한국어 이름 (프롬프트 문장에 쓰임)
   - 표에 없는 함수는 `_default`(접수·변경 + 확인 필요)로 막힌다. 빠뜨리면 조회도 막히니 반드시 전부 적는다.
   - `confirm_template` (접수·변경, 선택): 관문이 "동의 없음"으로 막았을 때 모델을 다시 부르지 않고 바로 말할 확인 문장. 인자 이름을 `{phone_number}`처럼 넣는다. 매뉴얼 말투로 쓴다.
4. **인계 신호 고치기** (`handoff`): 매뉴얼의 인계 기준에 맞춰 키워드와 이름을 고친다. 지금은 메모만 붙이고 판단은 모델이 한다.
5. **동의·거절 단어 확인** (`consent`): 매뉴얼 말투와 연습 음성에 맞지 않으면 고친다.
6. **고정 문장 바꾸기** (`*_phrases.json`): 브랜드 이름, 오류·지연 안내, `unverified_fallback`(출력 검사가 막을 때 말할 문장)을 매뉴얼 말투로.
   - 지침 글에 `{now}`를 넣지 않는다. 현재 시각·발신자·이력은 시스템 메시지 끝의 [상담 정보]로 자동으로 붙는다(고정 부분이 매 통화 같아야 프롬프트 캐시가 맞음).
7. **history 형식 맞추기**: 공식 `session.start`의 history 모양에 맞춰 `team/agent/policy.py`의 `normalize_history` 한 함수만 고친다. 상담원 말이 history에 없으면 확인 판정(`confirm: explicit`) 방식을 다시 정해야 한다.
8. **시험 문항 갱신과 재실행**: 공식 연습 문항으로 `team/data/` 시험 문항을 바꾸고 아래를 돌린다.
   ```
   .venv/Scripts/python -m pytest team/tests -q
   .venv/Scripts/python -m team.scripts.llm_probe --suite gate --repeat 3
   .venv/Scripts/python -m team.scripts.llm_probe --suite gate --full          # 함수 결과 뒤의 답까지
   .venv/Scripts/python -m team.scripts.llm_probe --case <문항> --show-prompt   # 조립된 프롬프트 눈으로 확인
   ```
