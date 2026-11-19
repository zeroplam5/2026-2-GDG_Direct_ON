# 튜닝에 쓰지 않는 테스트셋 (holdout_ko)

연습 문항(`practice_ko`, K01~K08)은 프롬프트와 발화 종료 설정을 고치면서 계속 봤기 때문에, 그 점수는 실제보다 좋게 나올 수 있습니다(REPORT '과적합 가능성').
이 세트는 **고친 결과가 처음 보는 말에도 통하는지** 확인하는 용도입니다. 공식 문항이 아닙니다.

## 규칙
1. **이 세트를 보면서 프롬프트·임계값·키워드를 고치지 않습니다.** 특정 H 문항을 통과시키려고 규칙을 넣으면 이 세트의 의미가 없어집니다.
2. 측정은 **큰 변경 뒤에만** 돌립니다 (예: 공식 매뉴얼 반영 후, 발화 종료 설정 변경 후, 최종 모델 결정 전).
3. 결과는 요약(끼어듦 수, 첫 소리 p50/p90, 함수 호출 일치, 기대 동작 충족 수)만 공유하고, 실패 사례는 `team/docs/ISSUES.md`에 적습니다.
4. 이 세트로 무언가를 고쳤다면, 그 문항은 `practice_ko`로 옮기고 새 문장으로 바꿉니다.
5. 녹음은 **연습 음성을 만들지 않은 사람**이 합니다. 가능하면 2명 이상.

## 녹음
- `script.tsv`의 문장을 한 문항에 파일 하나씩 녹음합니다. `|`가 있는 곳에서 괄호 안의 시간만큼 쉽니다.
- 휴대폰 녹음 앱이면 충분합니다(m4a, mp3, wav 등). 말 앞뒤로 1초쯤 조용히 둡니다.
- 파일 이름: `H01.m4a` 또는 녹음한 사람 구분 시 `H01_민수.m4a`, `H01_주연.m4a`
- 녹음 파일은 `team/data/holdout_ko/recordings/`에 둡니다. **원본 녹음은 커밋하지 않습니다** (목소리 개인정보). 변환된 16kHz 파일만 커밋할지는 팀에서 정합니다.

## 변환
```bash
.venv/Scripts/python -m team.scripts.make_testset team/data/holdout_ko/recordings --name holdout_ko --script team/data/holdout_ko/script.tsv --variants phone,noise
```
- 결과: `team/data/holdout_ko/audio/*.wav`, `audio/meta.json`, `cases.jsonl` (스타터킷 측정기 형식)
- 말 시작·끝은 음량으로 자동으로 찾습니다. **자동 말 끝은 꼭 들어 보고 확인**하고, 틀리면 `meta.json`의 숫자를 직접 고칩니다.
- `--variants phone`: 전화 음질 버전 (300~3400Hz, 8kHz 왕복). 문항 ID 뒤에 `P`
- `--variants noise`: 배경 잡음 버전 (SNR 15dB). 문항 ID 뒤에 `N`
- 대상 문항은 `--variant-ids` (기본 H01,H02,H03,H04,H08: 쉼·번호가 있는 문항)

## 측정
```bash
.venv/Scripts/python -m team.mock.mock_api --port 8100 &
.venv/Scripts/python -m team.scripts.bench --llm openai_compat --configs default_1100_1800 --repeats 2 \
  --cases team/data/holdout_ko/cases.jsonl --audio team/data/holdout_ko/audio --out team/runs/holdout_<날짜>
```
- 함수 호출 기대값은 연습용 모의 데이터(`data/demo_mock_data.json`의 DEMO1001, DEMO2002, 01000001234) 기준입니다.
- 기대 동작 충족 여부는 `results.jsonl`의 응답 텍스트와 함수 호출을 `script.tsv`의 '기대 동작'과 비교해 사람이 판정합니다.
