# 측정·운영: 외부 접속 · 동시 4연결 · 제출 압축

2026-11-19 · 담당 조여정 · 브랜치 `feature/measure-ops`
운영진 요구 사항(대회 사이트 '구현 범위·준수사항', '제출·일정') 중 측정·운영에 해당하는 항목을 점검했습니다.

## 요약
| 요구 사항 | 점검 결과 | 남은 일 |
| --- | --- | --- |
| 운영진 노트북이 **두 서버**(상담원, 모의 함수)에 접속 | 점검 스크립트 작성. 같은 네트워크 조건에서 5개 항목 통과 확인. **모의 서버 기본 주소가 `127.0.0.1`이라 그대로 띄우면 밖에서 접속 실패** | 노트북에서 터널(cloudflared/ngrok)로 다시 확인 |
| 문항 4개 동시 처리 | 1개씩 vs 4개 동시: 끼어듦·오류·`turn.end` 누락 0, 첫 소리 차이 없음, 답 내용 섞임 0 (대체물 기준) | 실제 API 모델로 다시 측정 (API 쪽 지연·속도 제한) |
| 제출 소스 압축 (패키지 목록, 시작 명령, 환경 변수 안내) | **맥에서 `team/requirements.txt` 설치 실패** 확인 → API용 설치 목록 추가. 새 가상환경에서 설치·테스트·서버 실행 확인. `team/env.example` 추가 | 최종 커밋 후 압축 |

## 1. 외부 접속

### 점검 스크립트
운영진에게 줄 두 주소로, **다른 컴퓨터에서** 실행합니다.
```bash
.venv/Scripts/python -m team.scripts.check_external --agent ws://노트북IP:9000 --mock http://노트북IP:8100
.venv/Scripts/python -m team.scripts.check_external --agent wss://터널주소 --mock https://터널주소
```
| 항목 | 확인 내용 |
| --- | --- |
| 1 | 모의 서버 `GET /tools` → 함수 목록 |
| 2 | 모의 서버 `POST /tools/{name}` + `X-Session-Id` → JSON |
| 3 | 정의에 없는 함수 → 404 |
| 4 | 스타터킷 측정기(수정 없음)로 상담원 서버 한 턴: 소리 수신, `turn.end`, 끼어듦 없음. 함수 문항 1개 + 일반 문항 1개 |
| 5 | 상담원이 부른 함수가 세션 ID와 함께 모의 서버에 기록됨 |

실패하면 원인 힌트를 함께 출력합니다 (연결 거부 → `127.0.0.1`로 떠 있음, 시간 초과 → 방화벽·와이파이, TLS 오류 → http/https 혼동).

### 점검 결과 (2026-11-19, 리눅스 컨테이너, 대체물 provider)
| 조건 | 결과 |
| --- | --- |
| 모의 서버 `--host 0.0.0.0`, 다른 주소(컨테이너 IP)로 접속 | 1~5 모두 통과. 첫 소리 1.21초(K01) / 1.14초(K07) |
| 모의 서버 기본값(`--host` 생략 = `127.0.0.1`) | **1번부터 실패: 연결 거부** |

- 상담원 서버는 기본이 `0.0.0.0`이라 문제없습니다. **모의 서버만 `--host 0.0.0.0`을 꼭 붙여야 합니다.**
- 터널은 이 환경에서 쓸 수 없어 확인하지 못했습니다. 노트북에서 해야 합니다.
- 공식 측정에서는 운영진 측 함수 중계가 우리 모의 서버를 부르고, 상담원은 `tools_url`(중계)을 부릅니다. 4번은 상담원이 외부 `tools_url`로 나가는 호출도 확인합니다.

### 측정 당일 절차 (안)
1. 시험용 데이터 파일을 받으면 모의 서버를 그 파일로 다시 띄운다
   `.venv/Scripts/python -m team.mock.mock_api --host 0.0.0.0 --port 8100 --data 받은파일.json --tools 공식함수정의.json`
2. 상담원 서버를 띄운다: `.venv/Scripts/python -m team.agent.server --port 9000`
3. 같은 와이파이면 노트북 IP, 막히면 터널 두 개를 연다
   `cloudflared tunnel --url http://localhost:9000`, `cloudflared tunnel --url http://localhost:8100`
4. **팀원 노트북에서** `check_external`로 두 주소를 확인한다 (1~5 통과)
5. 두 주소를 운영진에게 준다. 상담원은 `ws://`/`wss://`, 모의 서버는 `http://`/`https://`
6. 측정 중에는 서버를 고치거나 다시 띄우지 않는다. 노트북 절전·화면 잠금 끄기, 전원 연결

## 2. 동시 4연결
```bash
.venv/Scripts/python -m team.scripts.check_concurrency --out team/runs/concurrency_fake          # 대체물
.venv/Scripts/python -m team.scripts.check_concurrency --real --out team/runs/concurrency_real   # 실제 모델
```
상담원 서버 하나에 스타터킷 측정기를 `--concurrency 1`과 `--concurrency 4`로 번갈아 돌리고, 4개 동시일 때 답 내용이 1개씩일 때와 같은지도 비교합니다.

### 결과 (대체물: 가짜 STT, 스크립트 LLM, 신호음 TTS · 연습 음성 8문항 × 3회)
| 동시 연결 | 문항×회 | 끼어듦 | 오류 | turn.end 누락 | 첫 소리 p50 / p90 | 응답 완료 p50 / p90 | 답 내용이 1개씩일 때와 다름 |
|---|---|---|---|---|---|---|---|
| 1 | 24 | 0 | 0 | 0 | 1.91 / 1.91 s | 1.91 / 1.93 s | 0 |
| 4 | 24 | 0 | 0 | 0 | 1.91 / 1.91 s | 1.91 / 1.93 s | 0 |

- 서버 로그로 확인한 동시 처리 세션 수: 1개씩 단계 최대 1, 4개 동시 단계 최대 **4**. 실제로 겹쳐서 처리됐습니다.
- 서버 내부 처리(발화 종료 판단, 함수 호출, 송신)는 4개가 동시에 와도 느려지지 않고, 세션끼리 섞이지 않습니다.
- **이 측정은 API 쪽 지연을 포함하지 않습니다.** 실제 모델은 동시 요청 4개에서 응답이 느려지거나 속도 제한에 걸릴 수 있어 `--real`로 다시 재야 합니다.
- 가짜 STT는 소리와 상관없이 완성된 문장을 바로 주기 때문에, 문장 완결 판단을 끄고(`AGENT_SEMANTIC_EOU=0`) 무음 1.8초 기준으로 쟀습니다. 그래서 첫 소리가 1.9초 근처입니다. 속도 수치가 아니라 1개 vs 4개 비교용입니다.
- 원자료: `team/runs/concurrency_fake/`

## 3. 제출 압축
대회 요구: 상담원 서버·모의 함수 서버 소스, 실행에 필요한 패키지 목록, 시작 명령, 환경 변수 안내.

### 발견한 문제: 맥에서 설치 실패
`team/requirements.txt`의 GPU 패키지는 맥용 파일이 없어 `pip install -r team/requirements.txt`가 맥에서 실패합니다(PyPI 파일 목록으로 확인).

| 패키지 | 맥 | 윈도우 | 리눅스 |
| --- | --- | --- | --- |
| nvidia-cublas-cu12 | **없음** | O | O |
| nvidia-cudnn-cu12 | **없음** | O | O |
| 나머지 (ctranslate2, faster-whisper, webrtcvad-wheels, openai, comtypes) | O | O | O |

공식 모델은 모두 API라서 로컬 STT·GPU 패키지가 필요 없습니다. 그래서 기존 파일은 그대로 두고 **API용 설치 목록 `team/requirements-api.txt`**를 추가했습니다.

### 새 환경 점검 (리눅스, Python 3.13, 빈 가상환경)
| 단계 | 결과 |
| --- | --- |
| `pip install -r team/requirements-api.txt` | 성공 (패키지 27개) |
| `python -m pytest team/tests -q` | 39개 통과 |
| 모의 서버 + 상담원 서버 시작 → `check_external` | 모두 통과 |
| `git archive --format=zip -o submission.zip HEAD` | 파일 243개, 2.8MB. `.env`·`.venv` 없음 |

### 제출 시 안내할 내용
- 패키지: `team/requirements-api.txt` (로컬 STT까지 쓰면 `team/requirements.txt`, 맥 제외)
- 환경 변수: `team/env.example` (키 이름만, 값 없음)
- 시작 명령 (맥·리눅스는 `.venv/bin/python`, 윈도우는 `.venv/Scripts/python`)
  ```bash
  python -m venv .venv
  .venv/bin/pip install -r team/requirements-api.txt
  cp team/env.example .env        # 키 입력
  .venv/bin/python -m team.mock.mock_api --host 0.0.0.0 --port 8100
  .venv/bin/python -m team.agent.server --port 9000
  ```
- 압축: 최종 커밋 후 `git archive --format=zip -o submission.zip HEAD` (커밋한 파일만 들어가므로 `.env`가 섞이지 않음)
