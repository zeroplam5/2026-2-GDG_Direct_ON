# 사전 연습용 통신 규격 (공식 문제 자료 아님)

## WebSocket 상담원
- 포트: 예제 `9000` / URL `ws://localhost:9000`
- 요청 시작: JSON 텍스트 메시지 `session.start`가 먼저 전송됨
- 다음: 16kHz, 16-bit signed little-endian, mono PCM 바이너리 / 20ms 프레임(640byte)
- 고객이 말한 뒤에도 무음 프레임이 계속 옴: **발화 끝을 참가팀이 탐지**
- 응답: 같은 형식의 PCM 바이너리로 음성 전송. 종료시 `{"type":"turn.end"}` 텍스트 필수

예시 `session.start`:
```json
{"type":"session.start","session_id":"DEMO-1-abc","case_id":"DEMO1","now":"2026-11-21 11:00","history":[],"caller":null,"tools_url":"http://localhost:8100","audio":{"encoding":"pcm_s16le","sample_rate":16000,"channels":1}}
```

## Mock API 서버 인터페이스 (예제)
- `GET /tools`: 사용 가능한 함수의 메타데이터 반환
- `POST /tools/{name}`: JSON 입력, JSON 출력. `X-Session-Id` HTTP 헤더 포함
- `GET /calls?session_id=...`: 해당 세션의 함수 호출 기록(측정기가 참고)
- 위 경로들은 `eval/run_eval.py`가 사용하므로 연습용 Mock API 서버가 제공해야 함

## 평가의 기본 개념
- 측정기는 모든 팀에 똑같은 가상 음성을 전송하도록 만들 수 있음
- 각 팀은 자신의 코드를 자기 컴퓨터에서 실행함
- 응답 속도는 같은 방식으로 재지만 **팀별 하드웨어·네트워크/API 선택에 따라 달라질 수 있음**
- 본 스타터의 데모 측정 결과는 **정식 점수로 쓰지 않음**
