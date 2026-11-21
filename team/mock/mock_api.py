"""Practice mock function server (team implementation of mock_tools/mock_api_template.py).

Same routes and response shapes as the starter-kit skeleton:
    GET  /tools                      -> [{"type":"function","function":{...}}, ...]
    POST /tools/{name}  (X-Session-Id) -> recorded result for matching arguments
    GET  /calls?session_id=...       -> call log used by eval/run_eval.py

What is from the starter kit vs. our EXAMPLE choices (not official rules):
  * found     -> the "result" object stored in the data file          (starter kit data)
  * not found -> {"status":"not_found","message":...}, HTTP 200        (shape from docs/MOCK_API_TASK.md;
                                                                       message text is our choice)
  * unknown function -> HTTP 404 {"error":"unknown function"}         (same as the skeleton)
                        but ALSO recorded in /calls with "known": false (our choice: makes illegal
                        calls visible during practice; the skeleton did not record them)
  * missing required argument -> HTTP 200 {"status":"invalid"} recorded, matched=false (official status name)
  * OFFICIAL functions (SPEC.md, 2026-11-21), --match normalized (default):
      lookup  get_order_info, get_return_info: record whose phone OR order number matches
              (hyphens/spaces ignored), else {"status":"not_found"}
      accept  cancel_order, return_order, return_order_item, exchange_order, request_callback:
              record for the same number if any (e.g. "already_requested"), else {"status":"ok"};
              required value missing -> {"status":"invalid"}
      search  search_products: closest stored query (difflib ratio), even if not identical
  * other functions (starter-kit demo data): same arguments after trimming, case-folding and
    removing spaces/hyphens -> stored result, else not_found. --match strict: exact JSON equality
    for every function.

Data files are configurable so the official data (2026-11-21) can be dropped in:
    --tools PATH / MOCK_TOOLS_PATH   (default data/demo_tools.json)
    --data / --db PATH / MOCK_DATA_PATH  (default data/demo_mock_data.json; official: data/mock_db.json)
Data format assumed: {function_name: [{"arguments": {...}, "result": {...}}, ...]} (starter-kit format).

In the official environment the function relay records calls; /calls exists only for
compatibility with the starter-kit evaluator.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[2]
DEFAULT_TOOLS = REPO / "data" / "demo_tools.json"
DEFAULT_DATA = REPO / "data" / "demo_mock_data.json"


# Official function classes (SPEC.md 「모의 함수 서버가 할 일」, 2026-11-21)
OFFICIAL_KIND = {
    "get_order_info": "lookup", "get_return_info": "lookup",
    "cancel_order": "accept", "return_order": "accept", "return_order_item": "accept",
    "exchange_order": "accept", "request_callback": "accept",
    "search_products": "search",
}
# argument names that carry a phone number or an order number
ID_KEYS = {"order_id", "order_number", "buyer_cellphone", "customer_phone_number", "phone_number"}


def _id(v) -> str:
    """Phone / order number for comparison: hyphens, spaces and case ignored."""
    return re.sub(r"[\s\-]", "", str(v)).casefold() if isinstance(v, (str, int)) else ""


def _text(v) -> str:
    return re.sub(r"\s+", "", str(v or "")).casefold()


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
        self.tools = json.loads(self.tools_path.read_text(encoding="utf-8"))
        self.data = json.loads(self.data_path.read_text(encoding="utf-8"))
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    @property
    def tool_names(self):
        return {t["name"] for t in self.tools}

    def spec(self, name):
        return next((t for t in self.tools if t["name"] == name), None)

    def lookup(self, name, args):
        """Return (response_json_dict, matched_bool) - same contract as the skeleton.

        Official functions (SPEC.md 「모의 함수 서버」, 2026-11-21) follow the official rules
        (see _official); any other function (e.g. starter-kit demo data) keeps the old
        "same arguments -> stored result" behaviour."""
        spec = self.spec(name) or {}
        required = (spec.get("parameters") or {}).get("required") or []
        if not isinstance(args, dict):
            return {"status": "invalid", "message": "arguments must be a JSON object"}, False
        missing = [k for k in required if args.get(k) in (None, "", [], {})]
        if missing:
            return {"status": "invalid", "message": "missing: " + ", ".join(missing)}, False
        if self.match == "normalized" and name in OFFICIAL_KIND:
            return self._official(name, args)
        f = _norm if self.match == "normalized" else (lambda x: x)
        want = f(args)
        for rec in self.data.get(name, []):
            if f(rec.get("arguments")) == want:
                return rec["result"], True
        return {"status": "not_found", "message": "일치하는 데이터 없음 (예제 응답)"}, False

    def _official(self, name, args):
        rows = self.data.get(name, [])
        kind = OFFICIAL_KIND[name]
        ids = {k: _id(v) for k, v in args.items() if k in ID_KEYS and _id(v)}
        if kind == "lookup":
            # phone number OR order number of a stored record (hyphens / spaces ignored)
            if not ids:
                return {"status": "invalid", "message": "전화번호 또는 주문번호가 필요합니다"}, False
            # Several records for one number = snapshots from different dates; the server cannot
            # tell which one fits the call, so the first is used. On the 45 reference calls of the
            # practice data this reproduces 44; the one miss (P0177-09) differs only in empty
            # address fields, while "last record" would miss P0196-02 with a different 배송상태.
            for rec in rows:
                rid = {k: _id(v) for k, v in (rec.get("arguments") or {}).items() if k in ID_KEYS}
                if any(rid.get(k) == v for k, v in ids.items()):
                    return rec["result"], True
            return {"status": "not_found"}, False
        if kind == "accept":
            # a stored record for the same number wins (e.g. "already_requested"),
            # otherwise required values are present (checked above) -> ok
            for rec in rows:
                rargs = rec.get("arguments") or {}
                rid = {k: _id(v) for k, v in rargs.items() if k in ID_KEYS}
                same = (any(rid.get(k) == v for k, v in ids.items()) if ids and rid
                        else _norm(rargs) == _norm(args))
                if same:
                    return rec["result"], True
            return {"status": "ok"}, False
        # search: closest stored query, even if not identical
        q = _text(args.get("query", ""))
        best, score = None, -1.0
        for rec in rows:
            s = SequenceMatcher(None, q, _text((rec.get("arguments") or {}).get("query", ""))).ratio()
            if s > score:
                best, score = rec, s
        if best is None:
            return {"status": "not_found"}, False
        return best["result"], score >= 0.999

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
    p.add_argument("--data", "--db", dest="data", help="mock data file (--db as in the official start_mock.sh)")
    p.add_argument("--match", choices=["normalized", "strict"], default="normalized")
    a = p.parse_args()
    srv, store = serve(a.port, a.host, a.tools, a.data, a.match)
    print(f"practice mock API on http://{a.host}:{a.port} tools={store.tools_path} data={store.data_path} "
          f"match={a.match} functions={sorted(store.tool_names)}", flush=True)
    srv.serve_forever()
