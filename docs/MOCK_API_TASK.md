# Mock API는 직접 구현합니다 (가이드)

Mock API = 실제 쇼핑몰의 데이터베이스 없이, **미리 정의된 입력을 받으면 미리 정의된 출력을 돌려주는 간단한 HTTP 서비스**.

가상 예시:
- 입력: `POST /tools/get_order_info` `{"order_id":"DEMO1001"}`
- 출력: `{"status":"ok","data":{"order_id":"DEMO1001","shipping_status":"배송중"}}`
- 입력: `POST /tools/get_order_info` `{"order_id":"UNKNOWN"}`
- 출력: `{"status":"not_found","message":"주문 없음"}`

`mock_tools/mock_api_template.py`의 `lookup()` 함수를 수정해서 만들면 됩니다.
현재는 `not_implemented`를 반환하는 **골격**이므로 직접 구현하지 않으면 주문 조회 결과를 얻을 수 없습니다.
`data/demo_mock_data.json`에 가짜 데이터 2건, `data/demo_tools.json`에 예시 함수 정의 2개가 있습니다.

중요: 14개 공식 기능을 전부 구현해야 하는지, 행사 당일 공통 데이터와 최종 평가 서버를 어떻게 사용할지는 **기업·운영진 최종 확정 사항을 따릅니다.** 사전 연습용 예제의 범위를 공식 요건으로 오해하지 마세요.
