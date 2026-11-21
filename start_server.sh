#!/usr/bin/env bash
# 상담 서버 시작 (공식 README 7장). 운영진이 압축을 푼 폴더에서 `bash start_server.sh` 로 실행한다.
# 9000번, 0.0.0.0. 모델·키는 같은 폴더의 .env 에서 읽는다 (이미 있는 환경 변수가 우선).
cd "$(dirname "$0")"
exec python3 -m team.agent.server --host 0.0.0.0 --port 9000
