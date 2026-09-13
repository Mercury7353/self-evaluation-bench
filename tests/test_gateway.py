import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from seb.ledger import Ledger, BudgetExceeded
from seb.gateway import reservation, usage_from_wire, cost


def test_atomic_reservation_and_restart(tmp_path):
    ledger = Ledger(tmp_path / 'ledger.sqlite')
    ledger.wallet('test', 1)
    def reserve(i):
        try:
            ledger.reserve(str(i), 'test', 'm', .3)
            return True
        except BudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=10) as pool:
        assert sum(pool.map(reserve, range(20))) == 3
    assert Ledger(tmp_path / 'ledger.sqlite').status()[0]['outstanding'] == pytest.approx(.9)
    with pytest.raises(ValueError): ledger.wallet('test', 20)


def test_budget_is_released_only_on_known_charge(tmp_path):
    ledger = Ledger(tmp_path / 'ledger.sqlite'); ledger.wallet('test', 1)
    ledger.reserve('a', 'test', 'm', .9)
    ledger.finish('a', None, {}, 'interrupted')
    with pytest.raises(BudgetExceeded): ledger.reserve('b', 'test', 'm', .2)
    ledger.finish('a', .1, {'output_tokens': 2}, 'complete')
    ledger.reserve('c', 'test', 'm', .9)


def test_full_sse_usage_and_large_content():
    long = '完整轨迹' * 50000
    events = [{'type':'message_start','message':{'usage':{'input_tokens':100,'cache_read_input_tokens':20}}},
              {'type':'content_block_delta','delta':{'text':long}},
              {'type':'message_delta','usage':{'output_tokens':200}}, {'type':'message_stop'}]
    raw = b''.join(('data: '+json.dumps(e,ensure_ascii=False)+'\n\n').encode() for e in events)
    usage, complete = usage_from_wire(raw, True)
    assert complete and usage['output_tokens'] == 200
    assert long in raw.decode()
    assert cost(usage, {'input':10,'output':50}) == pytest.approx(.0112)
    assert not usage_from_wire(raw.rsplit(b'data:',1)[0], True)[1]


def test_reservation_rejects_unbounded_and_multimodal():
    price = {'input':10,'output':50}
    with pytest.raises(ValueError): reservation({'messages':[]}, price)
    with pytest.raises(ValueError): reservation({'max_tokens':100,'messages':[{'type':'image'}]},price)
    assert reservation({'max_tokens':100,'messages':[]},price) > .005


def test_proxy_persists_exact_large_wire_without_auth(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from fastapi.testclient import TestClient
    from seb.gateway import create_app
    payload = ('data: '+json.dumps({'type':'content_block_delta','delta':{'text':'Z'*1200000}})+'\n\n' +
               'data: '+json.dumps({'type':'message_start','message':{'usage':{'input_tokens':1,'output_tokens':1}}})+'\n\n'+
               'data: {"type":"message_stop"}\n\n').encode()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            for i in range(0,len(payload),317):self.wfile.write(payload[i:i+317])
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    Thread(target=server.serve_forever,daemon=True).start()
    key=tmp_path/'key';key.write_text('UPSTREAM_SECRET_MUST_NOT_APPEAR')
    config={'artifacts':str(tmp_path/'trace'),'key_file':str(key),'upstream':f'http://127.0.0.1:{server.server_port}',
            'prices':{'m':{'input':1,'output':1}},'tokens':{'client-secret':{'wallet':'w','cap':1,'models':['m']}}}
    body=b'{"model":"m", "max_tokens":100, "messages":[], "stream":true}'
    try:
        with TestClient(create_app(config)) as client:
            r=client.post('/anthropic/v1/messages',headers={'x-api-key':'client-secret'},content=body)
        assert r.status_code==200 and r.content==payload
        folder=tmp_path/'trace'/'wire'/r.headers['x-seb-request-id']
        assert (folder/'response.body').read_bytes()==payload
        assert (folder/'request.body').read_bytes()==body
        meta=json.loads((folder/'meta.json').read_text());assert meta['complete']
        for f in folder.iterdir():
            assert b'UPSTREAM_SECRET_MUST_NOT_APPEAR' not in f.read_bytes()
            assert b'client-secret' not in f.read_bytes()
    finally:server.shutdown()


def test_extra_paid_features_are_not_silently_unbudgeted():
    price={'input':1,'output':1}
    for extra in ({'n':3},{'best_of':4},{'service_tier':'priority'},{'tools':[{'type':'web_search_20250305','name':'web_search'}]},{'max_completion_tokens':300}):
        with pytest.raises(ValueError):reservation({'max_tokens':100,**extra},price)


def test_provider_route_overrides_have_no_single_model_reservation():
    for extra in ({'models':['other']}, {'fallbacks':'default'}, {'fallbacks':[{'model':'other'}]}):
        with pytest.raises(ValueError, match='frozen model route'):
            reservation({'model':'allowed','max_tokens':100,**extra},{'input':1,'output':1})


def test_researcher_alias_routes_to_provider_model(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from fastapi.testclient import TestClient
    from seb.gateway import create_app
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            raw=json.dumps({'type':'message','content':[],'usage':{'input_tokens':2,'output_tokens':1}}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);Thread(target=server.serve_forever,daemon=True).start()
    key=tmp_path/'key';key.write_text('fake-key')
    config={'artifacts':str(tmp_path/'gateway'),'key_file':str(key),'upstream':f'http://127.0.0.1:{server.server_port}',
        'prices':{'researcher':{'input':1,'output':1}},'model_backends':{'researcher':{'model':'provider/actual-model'}},
        'tokens':{'token':{'wallet':'designer','cap':1,'models':['researcher']}}}
    try:
        with TestClient(create_app(config)) as c:
            r=c.post('/anthropic/v1/messages',json={'model':'researcher','max_tokens':10,'messages':[]},headers={'x-api-key':'token'})
        assert r.status_code==200
        assert received[0]['model']=='provider/actual-model'
    finally:server.shutdown();server.server_close()


def test_cache_estimate_does_not_change_conservative_budget_charge():
    from seb.billing import cache_adjusted_cost
    usage={'input_tokens':100,'cache_read_input_tokens':900,'output_tokens':10}
    price={'input':2,'output':10,'cache_read_multiplier':.1}
    assert cost(usage,price)==pytest.approx(.0021)
    assert cache_adjusted_cost(usage,price)==pytest.approx(.00048)
    assert cache_adjusted_cost(usage,{'input':2,'output':10}) is None


@pytest.mark.parametrize('usage,expected', [
    ({'prompt_tokens':15,'completion_tokens':4,'total_tokens':260,
      'completion_tokens_details':{'reasoning_tokens':241},'prompt_tokens_details':None},.0022275),
    ({'prompt_tokens':109,'completion_tokens':23,'total_tokens':132,
      'completion_tokens_details':{'reasoning_tokens':18}},.0003705),
    ({'prompt_tokens':15,'completion_tokens':4,'total_tokens':19,
      'completion_tokens_details':None},.0000585),
])
def test_chat_reasoning_usage_counts_actual_output_once(usage, expected):
    from seb.billing import cache_adjusted_cost
    price={'input':1.5,'output':9}
    assert cost(usage,price)==pytest.approx(expected)
    assert cache_adjusted_cost(usage,price)==pytest.approx(expected)


@pytest.mark.parametrize('extra', [
    {'completion_tokens_details':{'reasoning_tokens':241}},
    {'total_tokens':999,'completion_tokens_details':{'reasoning_tokens':241}},
    {'total_tokens':19,'completion_tokens_details':{'reasoning_tokens':241}},
    {'total_tokens':19.5}, {'completion_tokens':-1},
])
def test_ambiguous_chat_usage_keeps_cost_unknown(extra):
    from seb.billing import cache_adjusted_cost
    usage={'prompt_tokens':15,'completion_tokens':4,**extra}
    price={'input':1.5,'output':9}
    assert cost(usage,price) is None
    assert cache_adjusted_cost(usage,price) is None


def test_expired_wallet_does_not_call_provider(tmp_path):
    from fastapi.testclient import TestClient
    from seb.gateway import create_app
    key=tmp_path/'key';key.write_text('fake-key')
    config={'artifacts':str(tmp_path/'gateway'),'key_file':str(key),'prices':{'m':{'input':1,'output':1}},
            'tokens':{'token':{'wallet':'test','cap':1,'models':['m'],'deadline_epoch':1}}}
    with TestClient(create_app(config)) as c:
        response=c.post('/anthropic/v1/messages',json={'model':'m','max_tokens':100,'messages':[]},headers={'x-api-key':'token'})
        assert response.status_code==409
        assert response.json()['error']['type']=='deadline_exceeded'
        assert c.get('/budget',headers={'x-api-key':'token'}).json()[0]['calls']==0
