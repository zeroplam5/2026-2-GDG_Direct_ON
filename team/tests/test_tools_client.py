import asyncio

import httpx

from team.agent.tools_client import ToolsClient, _normalize_tool_list


def test_normalize_tool_list_shapes():
    f = {"name": "a", "parameters": {"type": "object"}}
    assert _normalize_tool_list([{"type": "function", "function": f}])[0]["name"] == "a"
    assert _normalize_tool_list([f])[0]["name"] == "a"
    assert _normalize_tool_list({"tools": [f]})[0]["name"] == "a"


def test_undefined_function_is_never_sent(mock_server):
    url, store = mock_server

    async def go():
        async with httpx.AsyncClient() as http:
            tc = ToolsClient(http, url, "SID-U", 2.0)
            await tc.load()
            r1 = await tc.call("cancel_order", '{"order_id":"DEMO1001"}')
            r2 = await tc.call("get_order_info", "{}")
            r3 = await tc.call("get_order_info", "not json")
            r4 = await tc.call("get_order_info", '{"order_id":"DEMO1001"}')
            return r1, r2, r3, r4, tc.log

    r1, r2, r3, r4, log = asyncio.run(go())
    assert r1["error"] == "undefined_function"
    assert r2["error"] == "missing_arguments"
    assert r3["error"] == "invalid_arguments"
    assert r4["status"] == "ok"
    assert [c["name"] for c in store.calls_for("SID-U")] == ["get_order_info"]  # only the valid call reached the server
    assert [e["sent"] for e in log] == [False, False, False, True]


def test_uses_given_tools_url_and_header(mock_server):
    url, store = mock_server

    async def go():
        async with httpx.AsyncClient() as http:
            tc = ToolsClient(http, url + "/", "SID-H", 2.0)
            await tc.load()
            return await tc.call("request_callback", '{"phone_number":"01000001234"}')

    assert asyncio.run(go())["status"] == "ok"
    assert store.calls_for("SID-H")[0]["session_id"] == "SID-H"


def test_unreachable_server_returns_error_not_success():
    async def go():
        async with httpx.AsyncClient() as http:
            tc = ToolsClient(http, "http://127.0.0.1:9", "S", 1.0)
            tools = await tc.load()
            return tools, tc.list_error, await tc.call("get_order_info", '{"order_id":"x"}')

    tools, err, r = asyncio.run(go())
    assert tools == [] and err and r["status"] == "error"
