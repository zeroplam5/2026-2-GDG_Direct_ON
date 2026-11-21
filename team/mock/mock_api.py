"""Mock function server (team implementation of the kit's mock_tools/mock_server.py).

Routes (official kit directon-2026-voice-cs, SPEC.md "모의 함수 서버"):
    GET  /tools                      -> [{"type":"function","function":{...}}, ...]  (= tools.json)
    POST /tools/{name}  (X-Session-Id) -> result JSON
    GET  /calls?session_id=...       -> our own call log (debugging; the official evaluator's
                                        relay records calls and adds the per-function delay)

Responses (--match normalized, the default) follow SPEC.md:
  * read functions (get_order_info, get_return_info): the record whose phone / order number
    equals one given in the call (hyphens, spaces, case ignored); get_order_info also finds a
    record whose result lists that order number. None -> {"status":"not_found"}.
    No number at all -> {"status":"invalid"}.
  * write functions (cancel_order, return_order, return_order_item, exchange_order,
    request_callback and any other function): required values missing -> {"status":"invalid"};
    a record for the same number -> its result (e.g. "already_requested"); otherwise
    {"status":"ok"} with the message used by the data's ok records.
  * search_products: the record whose query (or product names) is closest to the query
    (character bigram overlap). Nothing in common -> {"status":"not_found"}.
  * unknown function -> HTTP 404 {"error":"unknown function"}, also logged with "known": false.
  * --match strict: the old exact-JSON-equality matching (practice tests only).

Data files:
    --tools PATH / MOCK_TOOLS_PATH            (default team/data/demo/demo_tools.json)
    --db PATH (= --data) / MOCK_DATA_PATH     (default team/data/demo/demo_mock_data.json)
Official run: bash start_mock.sh <mock_db_test.json> -> mock_tools/mock_server.py (kit skeleton) uses MockStore
Data format: {function_name: [{"arguments": {...}, "result": {...}}, ...]}.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[2]
DEFAULT_TOOLS = REPO / "team" / "data" / "demo" / "demo_tools.json"
DEFAULT_DATA = REPO / "team" / "data" / "demo" / "demo_mock_data.json"


READ_FUNCTIONS = {"get_order_info", "get_return_info"}
SEARCH_FUNCTIONS = {"search_products"}
# arguments that say whose record it is (same list as the official scorer, eval/score.py ID_ARGS)
ID_ARGS = ("order_id", "buyer_cellphone", "customer_phone_number", "phone_number", "order_number")
NOT_FOUND_MESSAGE = {"get_order_info": "주문 내역이 없습니다.", "get_return_info": "반품 상품이 없습니다.",
                     "search_products": "검색 결과가 없습니다."}
OK_MESSAGE = {"cancel_order": "주문 취소 접수 성공", "return_order": "주문 반품 접수 성공",
              "return_order_item": "상품 반품 접수 성공", "exchange_order": "주문 교환 접수 성공",
              "request_callback": "고객 정보 접수 성공"}


def _empty(v) -> bool:
    return v is None or (isinstance(v, (str, list, dict)) and not (v.strip() if isinstance(v, str) else v))


def _id(v) -> str:
    """Comparable phone / order number: letters and digits only, upper case."""
    return re.sub(r"[^0-9A-Za-z]", "", str(v)).upper()


def _ids(args: dict) -> dict:
    out = {k: _id(args[k]) for k in ID_ARGS if not _empty(args.get(k)) and _id(args[k])}
    codes = sorted(_id(i.get("order_item_code")) for i in (args.get("return_items") or [])
                   if isinstance(i, dict) and not _empty(i.get("order_item_code")))
    if codes:
        out["order_item_code"] = ",".join(codes)
    return out


def _grams(s: str) -> set:
    """Characters and character pairs, without spaces and the brand name every product shares."""
    s = re.sub(r"\s+|하루온", "", str(s or "")).casefold()
    return {s[i:i + 2] for i in range(len(s) - 1)} | set(s)


def _similarity(a: str, b: str) -> float:
    x, y = _grams(a), _grams(b)
    return 2 * len(x & y) / (len(x) + len(y)) if x and y else 0.0


def _norm(v):
    if isinstance(v, str):
        return re.sub(r"[\s\-]", "", v).casefold()
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


class MockStore:
    def __init__(self, tools_path, data_path, match="normalized"):
        self.tools_path, self.data_path, self.match = Path(tools_path), Path(data_path), match
        raw = json.loads(self.tools_path.read_text(encoding="utf-8"))
        self.tools = [t.get("function", t) for t in raw]  # bare functions or OpenAI "tools" entries
        self.data = json.loads(self.data_path.read_text(encoding="utf-8")) if self.data_path.is_file() else {}
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    @property
    def tool_names(self):
        return {t["name"] for t in self.tools}

    def spec(self, name):
        return next((t for t in self.tools if t["name"] == name), None)

    def lookup(self, name, args):
        """Return (response_json_dict, matched_bool): matched = a data record was used."""
        spec = self.spec(name) or {}
        required = (spec.get("parameters") or {}).get("required") or []
        if not isinstance(args, dict):
            return {"status": "invalid", "message": "arguments must be a JSON object"}, False
        missing = [k for k in required if _empty(args.get(k))]
        if missing:
            return {"status": "invalid", "message": "필수 값 없음: " + ", ".join(missing)}, False
        records = self.data.get(name, [])
        if self.match == "strict":
            for rec in records:
                if rec.get("arguments") == args:
                    return rec["result"], True
            return {"status": "not_found", "message": NOT_FOUND_MESSAGE.get(name, "일치하는 데이터 없음")}, False
        if name in SEARCH_FUNCTIONS:
            return self._search(name, args, records)
        ids = _ids(args)
        if name in READ_FUNCTIONS:
            if not ids:
                return {"status": "invalid", "message": "주문번호나 전화번호가 필요합니다."}, False
            return self._read(name, ids, records)
        return self._write(name, ids, records)

    def _read(self, name, ids, records):
        for rec in records:  # the number the customer gave is one of the record's arguments
            rid = _ids(rec.get("arguments") or {})
            if any(rid.get(k) == v for k, v in ids.items()):
                return rec["result"], True
        order = ids.get("order_id")
        if order and len(order) >= 10:  # a record found by phone whose result lists this order number
            # (10+ characters: a short fragment would match digits of dates / amounts)
            for rec in records:
                if order in _id(json.dumps(rec.get("result"), ensure_ascii=False)):
                    return rec["result"], True
        return {"status": "not_found", "message": NOT_FOUND_MESSAGE.get(name, "일치하는 데이터 없음")}, False

    def _write(self, name, ids, records):
        if ids:
            for rec in records:
                rid = _ids(rec.get("arguments") or {})
                same = [k for k in ids if k in rid]
                if same and all(rid[k] == ids[k] for k in same):
                    return rec["result"], True
        ok = next((r["result"] for r in records if (r.get("result") or {}).get("status") == "ok"), None)
        return dict(ok or {"status": "ok", "message": OK_MESSAGE.get(name, "접수 성공")}), False

    def _search(self, name, args, records):
        query = " ".join(str(v) for v in args.values() if isinstance(v, str))
        best, score = None, 0.0
        for rec in records:
            res = rec.get("result") or {}
            items = (res.get("data") or {}).get("결과") or [] if isinstance(res.get("data"), dict) else []
            names = " ".join(sorted({str(i.get("제품명", "")) for i in items if isinstance(i, dict)}))
            s = max(_similarity(query, (rec.get("arguments") or {}).get("query", "")), _similarity(query, names))
            if s > score:
                best, score = rec, s
        if best is None:
            return {"status": "not_found", "message": NOT_FOUND_MESSAGE.get(name, "검색 결과가 없습니다.")}, False
        return best["result"], True

    def record(self, sid, name, args, matched, known=True, status=None):
        with self.lock:
            self.calls.append({"session_id": sid, "name": name, "arguments": args, "matched": bool(matched),
                               "known": known, "status": status, "t_response": time.time()})

    def calls_for(self, sid):
        with self.lock:
            return [c for c in self.calls if sid is None or c["session_id"] == sid]


def make_handler(store: MockStore):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send_obj(self, obj, code=200):
            buf = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(buf)))
            self.end_headers()
            self.wfile.write(buf)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/tools":
                return self.send_obj([{"type": "function", "function": x} for x in store.tools])
            if u.path == "/calls":
                sid = parse_qs(u.query).get("session_id", [None])[0]
                return self.send_obj(store.calls_for(sid))
            return self.send_obj({"error": "not found"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            if not path.startswith("/tools/"):
                return self.send_obj({"error": "not found"}, 404)
            name = path.removeprefix("/tools/")
            sid = self.headers.get("X-Session-Id")
            try:
                n = int(self.headers.get("Content-Length", "0"))
                args = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, TypeError):
                store.record(sid, name, None, False, known=name in store.tool_names, status="invalid_json")
                return self.send_obj({"error": "invalid JSON"}, 400)
            if name not in store.tool_names:
                store.record(sid, name, args, False, known=False, status="unknown_function")
                return self.send_obj({"error": "unknown function"}, 404)
            result, matched = store.lookup(name, args)
            store.record(sid, name, args, matched, status=result.get("status"))
            return self.send_obj(result)

    return Handler


def serve(port=8100, host="127.0.0.1", tools=None, data=None, match="normalized"):
    store = MockStore(tools or os.environ.get("MOCK_TOOLS_PATH", DEFAULT_TOOLS),
                      data or os.environ.get("MOCK_DATA_PATH", DEFAULT_DATA), match)
    srv = ThreadingHTTPServer((host, port), make_handler(store))
    return srv, store


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8100)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--tools")
    p.add_argument("--db", "--data", dest="data", help="data file (official: mock_db.json / mock_db_test.json)")
    p.add_argument("--match", choices=["normalized", "strict"], default="normalized")
    a = p.parse_args()
    srv, store = serve(a.port, a.host, a.tools, a.data, a.match)
    print(f"practice mock API on http://{a.host}:{a.port} tools={store.tools_path} data={store.data_path} "
          f"match={a.match} functions={sorted(store.tool_names)}", flush=True)
    srv.serve_forever()
