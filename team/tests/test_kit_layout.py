"""The repository root follows the official kit (directon-2026-voice-cs) layout.

* start_mock.sh runs the kit's own command (mock_tools/mock_server.py --port 8100 --db <file>)
* mock_tools/mock_server.py is the kit skeleton with lookup() filled in (SPEC.md rules)
* eval/run_eval.py, eval/score.py, example_server/server.py are the kit originals
* server defaults: official pack, data/brand, official models
Made-up data only (kit data must not be published).
"""
from __future__ import annotations

import filecmp
import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from team.agent.config import Config

REPO = Path(__file__).resolve().parents[2]
KIT = REPO / "directon-2026-voice-cs"

TOOLS = [
    {"name": "get_order_info", "parameters": {"type": "object", "properties": {
        "order_id": {"type": "string"}, "buyer_cellphone": {"type": "string"}}, "required": []}},
    {"name": "request_callback", "parameters": {"type": "object", "properties": {
        "customer_name": {"type": "string"}, "phone_number": {"type": "string"}},
        "required": ["customer_name", "phone_number"]}},
]
DB = {"get_order_info": [{"arguments": {"order_id": None, "buyer_cellphone": "01055500000"},
                          "result": {"status": "ok", "data": {"orders": [{"주문번호": "20990101-0000001"}]}}}]}


def _load_kit_mock():
    spec = importlib.util.spec_from_file_location("kit_mock_server", REPO / "mock_tools" / "mock_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def kit_mock(tmp_path):
    (tmp_path / "tools.json").write_text(json.dumps(TOOLS, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "db.json").write_text(json.dumps(DB, ensure_ascii=False), encoding="utf-8")
    mod = _load_kit_mock()
    store = mod.Store(str(tmp_path / "db.json"), str(tmp_path / "tools.json"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.make_handler(store))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", store
    srv.shutdown()


def test_start_mock_uses_kit_command():
    sh = (REPO / "start_mock.sh").read_text(encoding="utf-8")
    assert 'mock_tools/mock_server.py --port 8100 --db "${1:-data/mock_db.json}"' in sh


def test_start_server_listens_on_9000_all_interfaces():
    sh = (REPO / "start_server.sh").read_text(encoding="utf-8")
    assert "--host 0.0.0.0" in sh and "--port 9000" in sh


def test_kit_mock_server_follows_spec(kit_mock):
    url, store = kit_mock
    tools = httpx.get(f"{url}/tools").json()
    assert [t["function"]["name"] for t in tools] == ["get_order_info", "request_callback"]
    h = {"X-Session-Id": "S1"}
    r = httpx.post(f"{url}/tools/get_order_info", json={"buyer_cellphone": "010-555-00000"}, headers=h).json()
    assert r["status"] == "ok"  # hyphens ignored
    assert httpx.post(f"{url}/tools/get_order_info", json={"order_id": "20990101-0000001"}, headers=h).json()["status"] == "ok"
    assert httpx.post(f"{url}/tools/get_order_info", json={"buyer_cellphone": "01099999999"}, headers=h).json()["status"] == "not_found"
    assert httpx.post(f"{url}/tools/request_callback", json={"customer_name": "가나", "phone_number": "01011112222"},
                      headers=h).json()["status"] == "ok"
    assert httpx.post(f"{url}/tools/request_callback", json={"customer_name": "가나"}, headers=h).json()["status"] == "invalid"
    assert httpx.post(f"{url}/tools/no_such_function", json={}, headers=h).status_code == 404
    calls = httpx.get(f"{url}/calls", params={"session_id": "S1"}).json()
    assert len(calls) == 5 and calls[0]["matched"] is True


@pytest.mark.skipif(not KIT.is_dir(), reason="official kit folder not present")
@pytest.mark.parametrize("rel", ["eval/run_eval.py", "eval/score.py", "example_server/server.py"])
def test_kit_tools_are_unmodified(rel):
    assert filecmp.cmp(REPO / rel, KIT / rel, shallow=False), f"{rel} differs from the kit original"


def test_server_defaults_are_official():
    cfg = Config()
    assert (cfg.stt_provider, cfg.soniox_model) == ("soniox", "stt-rt-v6")
    assert (cfg.tts_provider, cfg.gemini_tts_model) == ("gemini", "gemini-3.8-flash-lite-tts")
    assert cfg.llm_model == "gpt-5.4-mini"
    assert cfg.brand_dir == "data/brand"
    assert Path(cfg.rules_path).parent.name == "official"
