"""모의 함수 서버 — 공식 키트(directon-2026-voice-cs) mock_tools/mock_server.py 뼈대 그대로.

HTTP 주소 처리·인자·출력은 키트 원본과 같고, 비어 있던 lookup() 만 채웠다.
응답 고르는 규칙(SPEC.md 「모의 함수 서버」)은 team/mock/mock_api.py 의 MockStore 에 있다
(조회: 번호 일치 / 접수: 필수 값 있으면 ok / 검색: 가장 가까운 기록 / 없는 함수: 404).
호출 기록과 응답 지연은 측정기(run_eval.py)의 함수 중계가 맡는다.

실행:  python3 mock_tools/mock_server.py --port 8100 [--db data/mock_db.json]   (start_mock.sh 가 이렇게 부른다)

  GET  /tools                 함수 목록 (OpenAI tools 형식, data/brand/tools.json)
  POST /tools/{함수명}         body = 인수 JSON. 헤더 X-Session-Id 에 session.start 로 받은 session_id 가 온다.
  GET  /calls?session_id=...  (참고용) 이 서버가 받은 호출 기록. 채점에는 쓰지 않는다.
"""
import argparse, json, os, re, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # 저장소 맨 위 (team 패키지)
from team.mock.mock_api import MockStore  # noqa: E402


class Store:
    def __init__(self, db_path, tools_path):
        self._m = MockStore(tools_path, db_path)
        self.db = self._m.data          # {함수명: [{"arguments", "result"}]}
        self.tools = self._m.tools      # 함수 정의 (tools.json 의 function 부분)
        self.calls = []
        self.lock = threading.Lock()

    def lookup(self, name, args):
        """(응답 JSON, 기록과 맞았는지 bool). 규칙은 team/mock/mock_api.py MockStore.lookup."""
        return self._m.lookup(name, args)


def make_handler(store):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/tools":
                return self._send(200, [{"type": "function", "function": t} for t in store.tools])
            if u.path == "/calls":
                cid = parse_qs(u.query).get("session_id", [None])[0]
                with store.lock:
                    return self._send(200, [c for c in store.calls if cid is None or c["session_id"] == cid])
            self._send(404, {"error": "not found"})

        def do_POST(self):
            # 본문을 먼저 다 읽는다 (키트 원본과 다른 유일한 곳): 읽지 않고 404를 보내면 연결이
            # 끊겨(Windows WinError 10053) 중계가 404 대신 연결 오류를 받는다
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            m = re.fullmatch(r"/tools/(\w+)", urlparse(self.path).path)
            if not m or m.group(1) not in {t["name"] for t in store.tools}:
                return self._send(404, {"error": "unknown function"})
            name = m.group(1)
            t0 = time.time()
            try:
                args = json.loads(body or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "body must be JSON"})
            result, matched = store.lookup(name, args)
            # 참고용 호출 기록 (디버깅용)
            rec = {"session_id": self.headers.get("X-Session-Id"), "name": name, "arguments": args, "matched": matched,
                   "t_request": t0, "t_response": time.time()}
            with store.lock:
                store.calls.append(rec)
            self._send(200, result)
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--db", default=os.path.join(HERE, "..", "data", "mock_db.json"))
    ap.add_argument("--tools", default=os.path.join(HERE, "..", "data", "brand", "tools.json"))
    a = ap.parse_args()
    store = Store(a.db, a.tools)
    print(f"mock tools on :{a.port}  functions={len(store.tools)}  records={sum(map(len, store.db.values()))}",
          file=sys.stderr)
    ThreadingHTTPServer(("0.0.0.0", a.port), make_handler(store)).serve_forever()


if __name__ == "__main__":
    main()
