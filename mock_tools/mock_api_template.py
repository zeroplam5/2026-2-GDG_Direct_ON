"""DIRECT:ON pre-release: Mock API skeleton. Business logic is deliberately omitted."""
import argparse, json, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
TOOLS = json.loads((ROOT / 'data' / 'demo_tools.json').read_text(encoding='utf-8'))
MOCK_DATA = json.loads((ROOT / 'data' / 'demo_mock_data.json').read_text(encoding='utf-8'))
CALLS = []
LOCK = threading.Lock()

def lookup(name, args):
    """TODO: 참가팀이 미리 정의된 input -> output 매핑을 직접 구현할 부분.
    예: MOCK_DATA['get_order_info']에서 order_id를 찾아 result 반환.
    return (response_json_dict, matched_bool)
    """
    return {"status": "not_implemented", "message": "TODO: implement mock business logic"}, False

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass
    def send_obj(self, obj, code=200):
        buf=json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(buf)))
        self.end_headers()
        self.wfile.write(buf)
    def do_GET(self):
        u=urlparse(self.path)
        if u.path=='/tools':return self.send_obj([{'type':'function','function':x} for x in TOOLS])
        if u.path=='/calls':
            sid=parse_qs(u.query).get('session_id',[None])[0]
            with LOCK: rows=[c for c in CALLS if sid is None or c['session_id']==sid]
            return self.send_obj(rows)
        return self.send_obj({'error':'not found'},404)
    def do_POST(self):
        name=urlparse(self.path).path.removeprefix('/tools/')
        if name not in {x['name'] for x in TOOLS}:return self.send_obj({'error':'unknown function'},404)
        try:
            args=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
        except (ValueError,TypeError):return self.send_obj({'error':'invalid JSON'},400)
        result, matched=lookup(name,args)
        with LOCK:
            CALLS.append({'session_id':self.headers.get('X-Session-Id'), 'name':name,
                          'arguments':args, 'matched':bool(matched), 't_response':time.time()})
        return self.send_obj(result)

if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8100);a=p.parse_args()
    print(f'DEMO mock API skeleton on http://localhost:{a.port} (lookup TODO)',flush=True)
    ThreadingHTTPServer(('127.0.0.1',a.port),Handler).serve_forever()
