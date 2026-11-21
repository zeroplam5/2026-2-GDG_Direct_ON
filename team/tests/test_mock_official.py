"""Mock server rules of the official SPEC.md (「모의 함수 서버가 할 일」, 2026-11-21).

Uses made-up records in the official data SHAPE only ({name: [{"arguments", "result"}]}).
The official data is not copied into the repository (contest data must not leave the event)."""
import json

import httpx
import pytest

from team.mock.mock_api import MockStore, serve

TOOLS = [
    {"name": "get_order_info", "parameters": {"type": "object", "properties": {
        "order_id": {"type": "string"}, "buyer_cellphone": {"type": "string"}}, "required": []}},
    {"name": "get_return_info", "parameters": {"type": "object", "properties": {
        "customer_phone_number": {"type": "string"}}, "required": ["customer_phone_number"]}},
    {"name": "search_products", "parameters": {"type": "object", "properties": {
        "query": {"type": "string"}}, "required": ["query"]}},
    {"name": "cancel_order", "parameters": {"type": "object", "properties": {
        "order_id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["order_id", "reason"]}},
    {"name": "return_order", "parameters": {"type": "object", "properties": {
        "order_number": {"type": "string"}, "reason": {"type": "string"}}, "required": ["order_number", "reason"]}},
    {"name": "request_callback", "parameters": {"type": "object", "properties": {
        "customer_name": {"type": "string"}, "phone_number": {"type": "string"}},
        "required": ["customer_name", "phone_number"]}},
]
ORDER_A = {"status": "ok", "data": {"orders": [{"주문번호": "20990101-0000001", "배송상태": "배송중"}]}}
ORDER_A_LATER = {"status": "ok", "data": {"orders": [{"주문번호": "20990101-0000001", "배송상태": "배송완료"}]}}
DATA = {
    "get_order_info": [
        {"arguments": {"order_id": "20990101-0000001", "buyer_cellphone": "01000000001"}, "result": ORDER_A},
        {"arguments": {"order_id": None, "buyer_cellphone": "01000000001"}, "result": ORDER_A_LATER},
    ],
    "get_return_info": [{"arguments": {"customer_phone_number": "01000000002"}, "result": {"status": "not_found"}}],
    "search_products": [
        {"arguments": {"query": "관절 가드 2박스"}, "result": {"status": "ok", "data": {"결과": ["관절"]}}},
        {"arguments": {"query": "혈당 가드"}, "result": {"status": "ok", "data": {"결과": ["혈당"]}}},
    ],
    "return_order": [{"arguments": {"order_number": "20990101-0000001", "reason": "파손"},
                      "result": {"status": "already_requested"}}],
}


@pytest.fixture
def store(tmp_path):
    (tmp_path / "tools.json").write_text(json.dumps(TOOLS, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "db.json").write_text(json.dumps(DATA, ensure_ascii=False), encoding="utf-8")
    return MockStore(tmp_path / "tools.json", tmp_path / "db.json")


def test_lookup_by_order_number_or_phone_ignoring_hyphens_and_spaces(store):
    assert store.lookup("get_order_info", {"order_id": "20990101-0000001"}) == (ORDER_A, True)
    assert store.lookup("get_order_info", {"order_id": "209901010000001"}) == (ORDER_A, True)
    assert store.lookup("get_order_info", {"buyer_cellphone": "010-0000 0001"}) == (ORDER_A, True)
    # first record when several match (see the note in mock_api._official)
    assert store.lookup("get_order_info", {"order_id": None, "buyer_cellphone": "01000000001"})[0] == ORDER_A


def test_lookup_not_found_and_invalid(store):
    assert store.lookup("get_order_info", {"order_id": "20990101-9999999"}) == ({"status": "not_found"}, False)
    assert store.lookup("get_order_info", {})[0]["status"] == "invalid"  # no number at all
    assert store.lookup("get_return_info", {})[0]["status"] == "invalid"  # required value missing
    assert store.lookup("get_return_info", {"customer_phone_number": "010-0000-0002"}) == ({"status": "not_found"}, True)


def test_accept_is_ok_without_record_and_invalid_without_required(store):
    assert store.lookup("cancel_order", {"order_id": "X-1", "reason": "변심"}) == ({"status": "ok"}, False)
    assert store.lookup("request_callback", {"customer_name": "가", "phone_number": "01000000009"})[0] == {"status": "ok"}
    assert store.lookup("cancel_order", {"order_id": "X-1"})[0]["status"] == "invalid"
    assert store.lookup("cancel_order", {"order_id": "X-1", "reason": ""})[0]["status"] == "invalid"


def test_accept_returns_stored_record_for_same_number(store):
    r, matched = store.lookup("return_order", {"order_number": "2099-0101-0000001", "reason": "다른 사유"})
    assert r == {"status": "already_requested"} and matched


def test_search_returns_closest_record(store):
    r, exact = store.lookup("search_products", {"query": "관절가드 두 박스 주세요"})
    assert r["data"]["결과"] == ["관절"] and exact is False
    assert store.lookup("search_products", {"query": "혈당 가드"}) == ({"status": "ok", "data": {"결과": ["혈당"]}}, True)


def test_http_routes_and_db_option(tmp_path):
    (tmp_path / "tools.json").write_text(json.dumps(TOOLS, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "db.json").write_text(json.dumps(DATA, ensure_ascii=False), encoding="utf-8")
    srv, _ = serve(port=0, tools=str(tmp_path / "tools.json"), data=str(tmp_path / "db.json"))
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert [t["function"]["name"] for t in httpx.get(url + "/tools").json()] == [t["name"] for t in TOOLS]
        r = httpx.post(url + "/tools/cancel_order", json={"order_id": "X-2", "reason": "변심"}, headers={"X-Session-Id": "S"})
        assert r.json() == {"status": "ok"}
        assert httpx.post(url + "/tools/not_defined", json={}).status_code == 404
    finally:
        srv.shutdown()


def test_cli_accepts_db_like_official_start_mock():
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-m", "team.mock.mock_api", "--help"], capture_output=True, text=True)
    assert "--db" in out.stdout
