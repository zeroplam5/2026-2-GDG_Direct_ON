#!/usr/bin/env bash
# 모의 함수 서버 시작 (공식 README 7장). `bash start_mock.sh <데이터 파일>` (측정 때는 mock_db_test.json).
# 8100번, 0.0.0.0. 함수 정의는 공식 tools.json: 제출 압축의 official/tools.json (저장소에는 없음).
cd "$(dirname "$0")"
DB="${1:-official/mock_db.json}"
TOOLS="${MOCK_TOOLS_PATH:-official/tools.json}"
if [ ! -f "$TOOLS" ]; then
  echo "start_mock.sh: 함수 정의 파일이 없습니다: $TOOLS (team/scripts/make_submission.py 로 압축을 만들면 들어갑니다)" >&2
  exit 1
fi
exec python3 -m team.mock.mock_api --host 0.0.0.0 --port 8100 --db "$DB" --tools "$TOOLS"
