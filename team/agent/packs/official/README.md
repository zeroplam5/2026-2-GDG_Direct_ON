# 공식 묶음 (2026-11-21 당일 키트)

기본값으로 쓰이는 묶음이다. 연습 묶음은 `team/agent/prompts/`에 그대로 있다(테스트용).

| 파일 | 내용 | git |
|---|---|---|
| `prompt.md` | 우리 지침(말하기, 사실, 함수, 상담원에게 넘기기) | 커밋함 |
| `rules.json` | 공식 함수 8개의 관문 규칙, 인계 신호, 거절·보류 단어, 출력 검사 단어 | 커밋함 |
| `phrases.json` | 고정 문구(되묻기, 오류 안내 등) | 커밋함 |
| 키트의 `data/brand/` | 상담 시나리오, 브랜드 정보 96개, 말투 규칙 | **커밋하지 않음** |

키트 자료는 "대회 밖으로 옮기거나 공개하지 않는다"(README·DATA.md)라서 저장소에 넣지 않는다.
서버는 시작할 때 `AGENT_BRAND_DIR`(기본 `data/brand`, 저장소 루트 기준)에서 읽어 지침 뒤에 붙인다.
키트를 저장소 루트에 풀면 `data/brand/`에 들어가고, 그 경로는 `.gitignore`에 있다. 제출 압축 파일에는 그대로 들어간다.
폴더가 없으면 서버 로그에 경고가 남고 브랜드 자료 없이 동작한다.

## 시스템 프롬프트 순서

1. `prompt.md` (우리 지침)
2. `[상담 시나리오]`, `[브랜드 정보]`, `[말투 규칙]` (키트 `data/brand`, 링크·이모지 제거)
3. `[함수 실행 규칙]` (`rules.json`에서 자동 생성)
4. `[상담 정보]` 현재 시각, 발신자 정보, 이전 대화(함수 호출·결과 포함)

1~3은 통화마다 같아서 프롬프트 캐시에 걸리고, 4만 통화마다 바뀐다.

## 확인 방법

```bash
# 키 없이: 테스트
.venv/Scripts/python -m pytest team/tests -q
# 키 없이, 비용 없이: 연습 문항의 정답 호출이 관문을 통과하는지
.venv/Scripts/python -m team.scripts.llm_cases --kit <키트 폴더> --gate-only
# LLM 키(.env) 사용: 연습 문항 80개를 글자로 돌려 함수 점수(채점기와 같은 규칙) 보기
.venv/Scripts/python -m team.scripts.llm_cases --kit <키트 폴더> --limit 20
.venv/Scripts/python -m team.scripts.llm_cases --kit <키트 폴더> --only <case_id,...> --show
```

`--out`으로 답을 파일에 남길 수 있다. 키트 문장이 들어 있으니 `team/runs/llm_cases/`(git 제외) 아래에 둔다.
