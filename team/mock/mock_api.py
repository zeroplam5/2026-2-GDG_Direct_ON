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
  * missing required argument -> HTTP 200 {"status":"invalid_arguments"} recorded, matched=false (our choice)
  * argument matching: --match normalized (default) compares strings after trimming,
    case-folding and removing spaces/hyphens; --match strict requires exact JSON equality.
    Which rule the official data uses is NOT known.

Data files are configurable so the official data (2026-11-21) can be dropped in:
    --tools PATH / MOCK_TOOLS_PATH   (default data/demo_tools.json)
    --data  PATH / MOCK_DATA_PATH    (default data/demo_mock_data.json)
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[2]
DEFAULT_TOOLS = REPO / "data" / "demo_tools.json"
DEFAULT_DATA = REPO / "data" / "demo_mock_data.json"


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
        """Return (response_json_dict, matched_bool) - same contract as the skeleton."""
        spec = self.spec(name) or {}
        required = (spec.get("parameters") or {}).get("required") or []
        if not isinstance(args, dict):
            return {"status": "invalid_arguments", "message": "arguments must be a JSON object"}, False
        missing = [k for k in required if args.get(k) in (None, "")]
        if missing:
            return {"status": "invalid_arguments", "message": "missing: " + ", ".join(missing)}, False
        f = _norm if self.match == "normalized" else (lambda x: x)
        want = f(args)
        for rec in self.data.get(name, []):
            if f(rec.get("arguments")) == want:
                return rec["result"], True
        return {"status": "not_found", "message": "일치하는 데이터 없음 (예제 응답)"}, False

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
    p.add_argument("--data")
    p.add_argument("--match", choices=["normalized", "strict"], default="normalized")
    a = p.parse_args()
    srv, store = serve(a.port, a.host, a.tools, a.data, a.match)
    print(f"practice mock API on http://{a.host}:{a.port} tools={store.tools_path} data={store.data_path} "
          f"match={a.match} functions={sorted(store.tool_names)}", flush=True)
    srv.serve_forever()
