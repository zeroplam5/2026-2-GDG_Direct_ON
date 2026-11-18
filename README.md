# DIRECT:ON 2026 — 사전 기술연습용 STARTER KIT

> **사전 연습용 예제**입니다. 본 대회의 상담 데이터, 정식 매뉴얼, 연습 문항과 평가 문항은 **들어 있지 않습니다**.
> 아래 예시는 운영진이 독자적으로 만든 **가상 데이터**이며, 실제 대회 문제·정답·채점과 **무관**합니다.

## 목적
- WebSocket으로 **16kHz / 16-bit / mono PCM 음성**을 받아 응답할 수 있는지 연습
- Mock API 서버를 참가팀이 직접 만들고, AI와 HTTP 함수 호출을 연결하는 방법 연습
- 음성 종료 판단, 응답 시각 기록, `turn.end` 처리 확인

## 이 ZIP에 있는 것
- `example_server/server.py`: AI 없이 **삐 소리**만 반환하는 통신 예시 (정답 AI가 아님)
- `mock_tools/mock_api_template.py`: **직접 구현할** Mock API 서버의 기본 골격. 업무 로직은 TODO
- `eval/run_eval.py`: 주어진 샘플 음성을 흘려보내는 기술 측정기
- `eval/score_demo.py`: **속도와 통신만** 확인하는 오프라인 예제. 공식 점수 아님
- `data/demo_cases.jsonl` + `data/audio/demo/`: 가상 고객 발화 3개 (영어 합성음, 프로토콜 연습 전용)
- `data/demo_mock_data.json`, `data/demo_tools.json`: 예제 Mock API 입력/출력 연습 자료
- `docs/SPEC_STARTER.md`, `docs/MOCK_API_TASK.md`: 개발·테스트 안내

## 설치
Python 3.10 이상 권장. 가상환경에서:

```bash
python -m pip install websockets httpx numpy scipy
```

## 실행 — 터미널 3개
**반드시 ZIP 압축을 푼 폴더에서** 아래 명령을 실행하세요. 이전 측정 결과가 있다면 `runs/demo` 폴더를 삭제하거나 `--out runs/demo2`처럼 새 이름을 사용하세요.
**터미널 A — 참가자가 구현할 Mock API 서버 (골격)**
```bash
python mock_tools/mock_api_template.py --port 8100
```

**터미널 B — 통신만 시험하는 예시 상담원 (삐 소리만 남)**
```bash
python example_server/server.py --port 9000
```

**터미널 C — 가상 음성 3개 테스트**
```bash
python eval/run_eval.py --server ws://127.0.0.1:9000 \
  --cases data/demo_cases.jsonl --audio data/audio/demo \
  --mock http://127.0.0.1:8100 --out runs/demo --limit 3
python eval/score_demo.py --run runs/demo
```

처음 실행 시 `score_demo.py`의 응대 점수와 AI 모델 성능은 측정되지 않습니다. 예시 상담원은 삐 소리만 내기 때문입니다.

## 참가자가 만들어야 하는 것
1. 실제 고객 음성을 알아듣고 정확하게 답하는 AI 상담원 (WebSocket, 9000번 포트)
2. 주문 조회 등 **Mock API 서버의 기능을 스스로 구현** (HTTP, 예제 8100번 포트)
3. 요청/응답·세션 연결·상담원 인계 판단, 품질과 속도 개선

## 절대로 혼동하지 마세요
- 이 예제의 함수·고객 발화·주문번호는 **공식 문제 명세와 다를 수 있습니다.**
- 정식 문제·응대 매뉴얼·브랜드 데이터·숨겨진 시험 문항은 행사 운영진의 별도 안내가 기준입니다.
- **공식 채점 395문항 / 응대 50%·함수 20%·속도 30%를 이 ZIP만으로 재현할 수 없습니다.**
- 모델/API 선택, 비용 지원, Mock API 계약, 평가 서버와 제출물 세부 규격은 행사 운영 안내가 최종 기준입니다.

## 확인할 결과와 주의점
- 정상이면 `DEMO1`, `DEMO2`, `DEMO3`가 출력되고 `runs/demo/results.jsonl` 및 `runs/demo/agent_audio/*.wav`가 생성됩니다.
- 예시 상담원은 한국어 상담 기능이 없고, 가상 음성 3건은 **영어 합성음**입니다. 이는 음성 인식 정확도 테스트가 아니라 **통신 연결 테스트**입니다. 한국어 STT/TTS 품질은 참가팀이 별도로 준비한 음성으로 테스트해야 합니다.
- Mock API 서버의 `lookup()`은 **일부러 미완성(TODO)**이며 요청 시 `not_implemented`가 반환됩니다. 직접 구현하는 부분입니다.
- `run_eval.py`는 기본적으로 **이미 완료된 문항을 다시 측정하지 않습니다.** 다시 시험할 땐 새 출력 폴더를 사용하세요.
- **팀별 함수 규격·평가 서버·실제 상담 데이터는 아직 행사 운영진이 확정할 사항이며, 이 문서가 확정된 심사 규칙을 대신하지 않습니다.**
