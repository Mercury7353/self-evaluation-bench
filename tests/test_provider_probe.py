import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from seb.provider_probe import probe
from seb.execution_policy import DEFAULT_POLICY
from seb.ledger import Ledger


def test_probe_persists_usage_and_never_repeats_completed_answers(tmp_path):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(body)
            raw = json.dumps({'type':'message', 'stop_reason':'end_turn',
                'content':[{'type':'text','text':'READY'}], 'usage':{'input_tokens':2,'output_tokens':1}}).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    secret = tmp_path/'key'; secret.write_text('only-local-fixture')
    cfg = {'artifacts':str(tmp_path/'gateway'), 'key_file':str(secret),
        'upstream':f'http://127.0.0.1:{server.server_port}',
        'prices':{'c1':{'input':1,'output':1}}, 'evaluation_policy':DEFAULT_POLICY,
        'tokens':{'local-token':{'wallet':'development','cap':1,'models':['c1']}}}
    try:
        result = asyncio.run(probe(cfg, 'local-token', tmp_path/'probe'))
        assert result['all_transport_ok'] and result['rows'][0]['nonempty_answer']
        assert len(calls) == 1
        asyncio.run(probe(cfg, 'local-token', tmp_path/'probe'))
        assert len(calls) == 1
        wallet = Ledger(tmp_path/'gateway/ledger.sqlite').status()[0]
        assert wallet['calls'] == 1 and wallet['outstanding'] == 0
        assert 'only-local-fixture' not in (tmp_path/'probe/result.json').read_text()
    finally: server.shutdown(); server.server_close()
