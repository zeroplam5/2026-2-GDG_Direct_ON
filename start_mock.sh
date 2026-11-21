#!/usr/bin/env bash
# 모의 함수 서버 시작 — 최종 측정 때 운영진이 `bash start_mock.sh <데이터 파일>` 로 실행한다 (공식 키트와 같은 명령).
# 첫 인자로 받은 데이터 파일(측정 때는 mock_db_test.json)을 읽어 8100번 포트(0.0.0.0)에서 받는다.
# 함수 정의는 data/brand/tools.json (키트 자료: git에는 없고 제출 압축에는 들어간다).
cd "$(dirname "$0")"
exec python3 mock_tools/mock_server.py --port 8100 --db "${1:-data/mock_db.json}"
