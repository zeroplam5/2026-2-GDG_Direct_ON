"""Mock server behaviour required by the task: match / no-match / undefined function /
session-id call log. Uses the starter-kit demo data (not official data)."""
import httpx


def test_tools_listing_is_openai_style(mock_server):
    url, _ = mock_server
    r = httpx.get(url + "/tools")
    names = [t["function"]["name"] for t in r.json()]
    assert r.status_code == 200 and names == ["get_order_info", "request_callback"]
    assert all(t["type"] == "function" for t in r.json())


def test_known_function_matching_input(mock_server):
    url, _ = mock_server
    r = httpx.post(url + "/tools/get_order_info", json={"order_id": "DEMO1001"}, headers={"X-Session-Id": "S1"})
    assert r.json() == {"status": "ok", "data": {"order_id": "DEMO1001", "shipping_status": "배송중"}}
    calls = httpx.get(url + "/calls", params={"session_id": "S1"}).json()
    assert len(calls) == 1 and calls[0]["matched"] is True and calls[0]["session_id"] == "S1"


def test_normalized_matching_is_example_choice(mock_server):
    url, _ = mock_server
    r = httpx.post(url + "/tools/request_callback", json={"phone_number": "010-0000-1234"}, headers={"X-Session-Id": "S2"})
    assert r.json()["status"] == "ok"


def test_known_function_no_matching_data(mock_server):
    url, _ = mock_server
    r = httpx.post(url + "/tools/get_order_info", json={"order_id": "DEMO9999"}, headers={"X-Session-Id": "S3"})
    assert r.status_code == 200 and r.json()["status"] == "not_found"
    c = httpx.get(url + "/calls", params={"session_id": "S3"}).json()
    assert c[0]["matched"] is False and c[0]["known"] is True


def test_missing_required_argument(mock_server):
    url, _ = mock_server
    r = httpx.post(url + "/tools/get_order_info", json={}, headers={"X-Session-Id": "S4"})
    assert r.json()["status"] == "invalid"  # official status name (SPEC.md, 2026-11-21)


def test_undefined_function_is_404_and_logged(mock_server):
    url, _ = mock_server
    r = httpx.post(url + "/tools/cancel_order", json={"order_id": "DEMO1001"}, headers={"X-Session-Id": "S5"})
    assert r.status_code == 404 and r.json() == {"error": "unknown function"}
    c = httpx.get(url + "/calls", params={"session_id": "S5"}).json()
    assert c[0]["known"] is False and c[0]["matched"] is False


def test_invalid_json(mock_server):
    url, _ = mock_server
    r = httpx.post(url + "/tools/get_order_info", content=b"{bad", headers={"X-Session-Id": "S6"})
    assert r.status_code == 400


def test_calls_are_isolated_by_session(mock_server):
    url, _ = mock_server
    for sid, oid in [("A", "DEMO1001"), ("B", "DEMO2002"), ("A", "DEMO2002")]:
        httpx.post(url + "/tools/get_order_info", json={"order_id": oid}, headers={"X-Session-Id": sid})
    a = httpx.get(url + "/calls", params={"session_id": "A"}).json()
    b = httpx.get(url + "/calls", params={"session_id": "B"}).json()
    assert [x["arguments"]["order_id"] for x in a] == ["DEMO1001", "DEMO2002"]
    assert [x["arguments"]["order_id"] for x in b] == ["DEMO2002"]
    # fields read by eval/run_eval.py
    assert {"name", "arguments", "matched", "t_response"} <= set(a[0])


def test_data_paths_are_configurable(tmp_path):
    import json
    from team.mock.mock_api import MockStore
    (tmp_path / "t.json").write_text(json.dumps([{"name": "f", "parameters": {"type": "object", "required": ["x"]}}]), encoding="utf-8")
    (tmp_path / "d.json").write_text(json.dumps({"f": [{"arguments": {"x": "1"}, "result": {"status": "ok"}}]}), encoding="utf-8")
    s = MockStore(tmp_path / "t.json", tmp_path / "d.json", match="strict")
    assert s.lookup("f", {"x": "1"}) == ({"status": "ok"}, True)
    assert s.lookup("f", {"x": " 1"})[1] is False
