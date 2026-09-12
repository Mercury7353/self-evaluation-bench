import json
import time
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from seb.ledger import Ledger
from seb.research import validate_result, validate_submission, research_app, scoped_cost


def test_outputs_require_current_wallet_evidence(tmp_path):
    ledger=Ledger(tmp_path/'ledger');ledger.wallet('one',1);ledger.wallet('two',1)
    start=time.time()-1;call='a'*32
    ledger.reserve(call,'one','model',.1);ledger.finish(call,.01,{},'completed')
    r={'score':.5,'items':[{'id':'i','score':.5,'evidence':[{'kind':'llm','id':call}]}]}
    assert validate_result(r,ledger,'one',start,tmp_path)==r
    with pytest.raises(ValueError):validate_result(r,ledger,'two',start,tmp_path)
    with pytest.raises(ValueError):validate_result(r,ledger,'one',time.time()+1,tmp_path)
    r['items'][0]['evidence']=[]
    with pytest.raises(ValueError):validate_result(r,ledger,'one',start,tmp_path)


def test_trial_cost_excludes_unrelated_calls_and_includes_unfinished_children(tmp_path):
    root=tmp_path/'gateway';ledger=Ledger(root/'ledger.sqlite');ledger.wallet('shared',2)
    for name,amount in [('a',.02),('b',.25)]:
        ledger.reserve(name*32,'shared','model',amount)
        ledger.finish(name*32,amount,{'input_tokens':10},'completed')
    scope='c'*32;index=root/'scopes'/scope;index.mkdir(parents=True)
    (index/('a'*32)).touch()
    result=scoped_cost({'artifacts':str(root)},scope)
    assert result['charged_usd']==pytest.approx(.02)
    assert result['calls']==1 and result['cost_complete']
    child='d'*32;(index/'jobs').mkdir();(index/'jobs'/child).touch()
    result=scoped_cost({'artifacts':str(root)},scope)
    assert result['pending_jobs']==[child] and not result['cost_complete']
    target=tmp_path/'research-jobs'/child;target.mkdir(parents=True)
    (target/'result.json').write_text(json.dumps({'status':'ok'}))
    assert scoped_cost({'artifacts':str(root)},scope)['cost_complete']


def test_fetch_is_not_available_to_evaluated_program(tmp_path):
    key=tmp_path/'key';key.write_text('secret')
    config={'key_file':str(key),'artifacts':str(tmp_path/'gateway'),
            'tokens':{'token':{'wallet':'eval','cap':2,'models':['m']}},'prices':{'m':{'input':1,'output':1}}}
    with TestClient(research_app(config)) as c:
        r=c.post('/research/fetch',json={'url':'https://example.org'},headers={'x-api-key':'token'})
        assert r.status_code==404


@pytest.mark.parametrize('limit',[0,-1,16385,True,'1024'])
def test_invalid_agent_generation_limits_are_rejected_before_execution(tmp_path,limit):
    key=tmp_path/'key';key.write_text('secret')
    config={'key_file':str(key),'artifacts':str(tmp_path/'gateway'),
            'tokens':{'token':{'wallet':'test','cap':2,'models':['m'],'workspace':str(tmp_path)}},
            'prices':{'m':{'input':1,'output':1}}}
    with TestClient(research_app(config)) as client:
        response=client.post('/research/agents',json={'path':'task','model':'m','max_output_tokens':limit},headers={'x-api-key':'token'})
        assert response.status_code==400
        assert not list((tmp_path/'research-jobs').iterdir())
        assert client.get('/budget',headers={'x-api-key':'token'}).json()[0]['calls']==0


def test_submission_rejects_outside_dependencies(tmp_path):
    (tmp_path/'run.py').write_text('pass');(tmp_path/'README.md').write_text('usage')
    assert 'run.py' in validate_submission(tmp_path)
    (tmp_path/'outside').symlink_to('/etc/passwd')
    with pytest.raises(ValueError):validate_submission(tmp_path)


def test_full_job_artifacts_are_wallet_scoped_and_private_files_are_excluded(tmp_path):
    key=tmp_path/'key';key.write_text('secret')
    config={'key_file':str(key),'artifacts':str(tmp_path/'gateway'),
            'tokens':{'one':{'wallet':'one','cap':2,'models':['m']},
                      'two':{'wallet':'two','cap':2,'models':['m']}},
            'prices':{'m':{'input':1,'output':1}}}
    ident='b'*32;folder=tmp_path/'research-jobs'/ident;execution=folder/'execution';execution.mkdir(parents=True)
    (folder/'request.json').write_text(json.dumps({'wallet':'one','model':'m'}))
    (execution/'claude.stdout').write_text('full untruncated trace')
    (execution/'launch.private.json').write_text('private')
    with TestClient(research_app(config)) as client:
        route='/research/jobs/'+ident+'/artifacts'
        assert client.get(route,headers={'x-api-key':'two'}).status_code==403
        assert client.get(route,headers={'x-api-key':'one'}).json()=={'files':['claude.stdout']}
        assert client.get(route+'/claude.stdout',headers={'x-api-key':'one'}).text=='full untruncated trace'
        assert client.get(route+'/launch.private.json',headers={'x-api-key':'one'}).status_code==403


def test_provider_undercount_cannot_reduce_reservation(tmp_path):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from threading import Thread
    from seb.gateway import create_app
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            payload=({'input_tokens':1268} if self.path.endswith('count_tokens') else
                     {'content':[{'type':'text','text':'ok'}],'usage':{'input_tokens':2787,'output_tokens':1500}})
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);Thread(target=server.serve_forever,daemon=True).start()
    key=tmp_path/'key';key.write_text('SECRET')
    config={'artifacts':str(tmp_path/'trace'),'key_file':str(key),'upstream':f'http://127.0.0.1:{server.server_port}',
            'count_input_tokens':True,'prices':{'m':{'input':10,'output':50,'cache_write_multiplier':1}},
            'tokens':{'client':{'wallet':'w','cap':2,'models':['m'],'trace_scopes':['a'*32,'b'*32]}}}
    # Match the live failure's count, billed input, and exhausted output limit.
    # The former count + 1024 rule reserves .09792, below the .10287 charge,
    # and incorrectly shuts this wallet down despite its $2 total cap.
    body={'model':'m','max_tokens':1500,'messages':[{'role':'user','content':'X'*6000}]}
    try:
        with TestClient(create_app(config)) as c:
            r=c.post('/anthropic/v1/messages',json=body,headers={'x-api-key':'client'})
            assert r.status_code==200
            wallet=c.get('/budget',headers={'x-api-key':'client'}).json()[0]
            assert wallet['charged']==pytest.approx(.10287)
            assert wallet['cap']==2
            assert wallet['outstanding']==0
        wire=tmp_path/'trace/wire'/r.headers['x-seb-request-id']
        assert (wire/'count.response.body').exists()
        meta=json.loads((wire/'meta.json').read_text())
        assert meta['reservation_method']=='max_byte_bound_provider_count'
        assert meta['reservation_usd'] >= meta['charge_usd']
        for scope in ['a'*32,'b'*32]:
            assert (tmp_path/'trace/scopes'/scope/r.headers['x-seb-request-id']).exists()
    finally:server.shutdown()
