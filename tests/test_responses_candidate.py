import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from seb.billing import cache_adjusted_cost
from seb.execution_policy import DEFAULT_POLICY
from seb.gateway import create_app
from seb.ledger import Ledger
from seb.provider_probe import probe
from seb.responses_candidate import CandidateAdapter


PRICE={'input':4,'output':20,'cache_read_multiplier':.1,'cache_write_multiplier':1.25,
       'long_context':{'threshold_tokens':272000,'input_multiplier':2,'output_multiplier':1.5}}


def answer(text='READY',**change):
    body={'id':'resp_test','status':'completed','model':'gpt-5.6-sol',
          'usage':{'input_tokens':100,'output_tokens':20,'input_tokens_details':{'cached_tokens':80}},
          'output':[{'type':'message','id':'msg_test','role':'assistant','status':'completed',
                     'phase':'final_answer','content':[{'type':'output_text','text':text,'annotations':[]}]}]}
    return body | change


def tool_answer():
    return answer(output=[
        {'type':'reasoning','id':'rs_test','summary':[],'encrypted_content':'opaque-provider-state'},
        {'type':'message','id':'msg_comment','role':'assistant','status':'completed','phase':'commentary',
         'content':[{'type':'output_text','text':'I will use the tool.','annotations':[]}]},
        {'type':'function_call','id':'fc_test','call_id':'call_test','name':'Bash',
         'arguments':json.dumps({'command':'echo CLI_TOOL_OK > marker.txt','description':'Write a local test marker'})}])


@pytest.fixture
def provider(tmp_path):
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append({'path':self.path,'body':body,'headers':dict(self.headers)})
            status,value=server.respond(body)
            raw=json.dumps(value).encode()
            self.send_response(status);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.respond=lambda body:(200,answer())
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    key=tmp_path/'fixture.key';key.write_text('FAKE_PROVIDER_SECRET')
    cfg={'artifacts':str(tmp_path/'gateway'),'key_file':str(key),'prices':{'c01':PRICE},
         'model_backends':{'c01':{'model':'gpt-5.6-sol','upstream':f'http://127.0.0.1:{server.server_port}/v1',
             'wire_api':'openai_responses','effort':'max','native_limits':{'max_output_tokens':128000,'max_context_tokens':1050000}}},
         'evaluation_policy':DEFAULT_POLICY | {'retry_backoff_seconds':0},
         'tokens':{'fixture-token':{'wallet':'development','cap':25,'models':['c01']}}}
    yield server,received,cfg
    server.shutdown();server.server_close();thread.join(timeout=2)


def payload(**change):
    return {'model':'c01','max_tokens':32768,'messages':[{'role':'user','content':'Hello'}],**change}


def post(client,operation='original',**change):
    return client.post('/anthropic/v1/messages',json=payload(**change),
        headers={'x-api-key':'fixture-token','x-seb-operation-id':operation})


def test_adapter_preserves_wires_and_native_cached_usage(provider):
    server,received,cfg=provider
    with TestClient(create_app(cfg)) as client:
        response=post(client,system='Keep this system instruction.')
        assert response.status_code==200 and response.json()['model']=='c01'
        replay=post(client,system='Keep this system instruction.')
        assert replay.headers['x-seb-replayed']=='true'
    assert len(received)==1 and received[0]['path']=='/v1/responses'
    wire=received[0]['body']
    assert wire['model']=='gpt-5.6-sol' and wire['reasoning']=={'effort':'max'}
    assert wire['max_output_tokens']==32768 and wire['store'] is False and wire['stream'] is False
    assert wire['input'][0]['content']=='Keep this system instruction.'
    folder=Path(cfg['artifacts'])/'wire'/response.headers['x-seb-request-id']
    assert json.loads((folder/'request.body').read_text())['model']=='c01'
    assert json.loads((folder/'upstream.request.body').read_text())==wire
    meta=json.loads((folder/'meta.json').read_text())
    assert meta['charge_usd']==pytest.approx(.0008)
    assert cache_adjusted_cost(meta['usage'],PRICE)==pytest.approx(.000512)
    assert all(b'FAKE_PROVIDER_SECRET' not in p.read_bytes() for p in folder.iterdir())


def test_tool_state_survives_restart_and_replays_commentary_phase_exactly(provider):
    server,received,cfg=provider
    server.respond=lambda body:(200,tool_answer() if len(received)==1 else answer())
    with TestClient(create_app(cfg)) as client: first=post(client).json()
    messages=payload()['messages']+[{'role':'assistant','content':first['content']},
        {'role':'user','content':[{'type':'tool_result','tool_use_id':'call_test','content':'tool output'}]}]
    with TestClient(create_app(cfg)) as client:
        response=client.post('/anthropic/v1/messages',json=payload(messages=messages,stream=True),
            headers={'Authorization':'Bearer fixture-token','x-seb-operation-id':'second'})
        assert response.status_code==200 and 'event: message_stop' in response.text
    assert received[1]['body']['input'][1:4]==tool_answer()['output']
    assert received[1]['body']['input'][-1]['type']=='function_call_output'
    backend=cfg['model_backends']['c01']
    isolated=CandidateAdapter(cfg['artifacts'],backend,'c01','other-session')
    other,_=isolated.prepare(payload(messages=messages),PRICE)
    assert 'opaque-provider-state' not in json.dumps(other)
    messages[1]['content'][-1]['input']['command']='changed'
    with TestClient(create_app(cfg)) as client:assert post(client,operation='mutated',messages=messages).status_code==400
    assert len(received)==2


@pytest.mark.parametrize('body',[answer('wrong'),answer(''),answer(output=[],status='incomplete'),
    answer(output=[{'type':'message','content':[{'type':'refusal','refusal':'declined'}]}])])
def test_completed_wrong_empty_truncated_and_refused_answers_are_never_retried(provider,body):
    server,received,cfg=provider;server.respond=lambda _:(200,body)
    with TestClient(create_app(cfg)) as client:
        response=post(client)
        assert response.status_code==200 and response.headers['x-seb-attempt-count']=='1'
    assert len(received)==1 and Ledger(Path(cfg['artifacts'])/'ledger.sqlite').status()[0]['outstanding']==0


@pytest.mark.parametrize('fault',['missing_usage','bad_tool_arguments','missing_encrypted_reasoning'])
def test_invalid_provider_response_retains_cost_without_retry(provider,fault):
    server,received,cfg=provider;body=tool_answer()
    if fault=='missing_usage':body.pop('usage')
    elif fault=='bad_tool_arguments':body['output'][-1]['arguments']='not json'
    else:body['output'][0].pop('encrypted_content')
    server.respond=lambda _:(200,body)
    with TestClient(create_app(cfg)) as client:response=post(client)
    assert response.status_code==422 and len(received)==1
    row=Ledger(Path(cfg['artifacts'])/'ledger.sqlite').status()[0]
    assert (row['outstanding']>0) if fault=='missing_usage' else (row['charged']>0)


def test_probe_stops_on_upstream_quota_and_does_not_revisit_models(provider,tmp_path):
    server,received,cfg=provider
    cfg['prices']['c02']=PRICE;cfg['model_backends']['c02']=dict(cfg['model_backends']['c01'])
    cfg['tokens']['fixture-token']['models'].append('c02')
    server.respond=lambda _:(429,{'error':{'code':'insufficient_quota'}})
    first=asyncio.run(probe(cfg,'fixture-token',tmp_path/'probe'))
    second=asyncio.run(probe(cfg,'fixture-token',tmp_path/'probe'))
    assert first==second and first['stopped_on_quota'] and len(received)==1
    assert first['stop_reason']=='configuration_or_quota_error'
    assert Ledger(Path(cfg['artifacts'])/'ledger.sqlite').status()[0]['outstanding']>0


def test_only_transient_failures_retry_with_unknown_costs_retained(provider):
    server,received,cfg=provider
    server.respond=lambda _:(503,{'error':{'type':'overloaded_error'}}) if len(received)<=3 else (200,answer())
    with TestClient(create_app(cfg)) as client:
        response=post(client)
        assert response.status_code==200 and response.headers['x-seb-attempt-count']=='4'
        assert post(client).headers['x-seb-replayed']=='true'
    row=Ledger(Path(cfg['artifacts'])/'ledger.sqlite').status()[0]
    assert len(received)==4 and row['calls']==4 and row['outstanding']>0 and row['charged']>0
    assert all(item['body']==received[0]['body'] for item in received)


def test_native_tool_context_crosses_long_context_billing_threshold(provider):
    _,_,cfg=provider
    price=dict(PRICE,reservation_output_headroom=1.1)
    adapter=CandidateAdapter(cfg['artifacts'],cfg['model_backends']['c01'],'c01','long-context')
    body=tool_answer()
    body['usage']={'input_tokens':250000,'output_tokens':20000,'input_tokens_details':{}}
    raw,_=adapter.translate(body,False);first=json.loads(raw)
    messages=payload()['messages']+[{'role':'assistant','content':first['content']},
        {'role':'user','content':[{'type':'tool_result','tool_use_id':'call_test','content':'done'}]}]
    wire,amount=adapter.prepare(payload(messages=messages),price)
    assert len(json.dumps(wire).encode())<272000
    # Opaque state pushes a small visible request past 272k. Both input and
    # output must receive the higher tariff, including output headroom.
    minimum=(270000*4*1.25*2 + 32768*1.1*20*1.5)/1e6
    assert amount>=minimum


def test_context_edits_and_effort_changes_are_rejected_before_api(provider):
    _,received,cfg=provider
    with TestClient(create_app(cfg)) as client:
        assert post(client,context_management={'edits':[{'type':'clear_thinking_20251015','keep':0}]}).status_code==400
        assert post(client,output_config={'effort':'low'}).status_code==400
        assert post(client,thinking={'type':'enabled','budget_tokens':1000}).status_code==400
    assert not received and Ledger(Path(cfg['artifacts'])/'ledger.sqlite').status()[0]['calls']==0


def test_real_claude_code_tool_roundtrip_with_responses_provider(provider,tmp_path):
    root=os.environ.get('SEB_TEST_ROOT')
    if not root or not shutil.which('claude'):pytest.skip('Set rootfs and install native Claude Code for tool integration')
    import uvicorn
    from seb.container import launch_claude
    server,received,cfg=provider
    server.respond=lambda body:(200,tool_answer() if len(received)==1 else answer('CLI_DONE'))
    app=create_app(cfg)
    requests=[]
    @app.middleware('http')
    async def capture(request,call_next):
        if request.url.path.endswith('/messages'):
            body=await request.json();requests.append(body)
            (tmp_path/f'cli-request-{len(requests)}.json').write_text(json.dumps(body,indent=2))
        return await call_next(request)
    image=tmp_path/'image';shutil.copytree(root,image,symlinks=True)
    work=tmp_path/'work';work.mkdir()
    with tempfile.TemporaryDirectory(prefix='seb-candidate-test-') as sockets:
        listener=socket.socket(socket.AF_UNIX)
        address=Path(sockets)/'gateway.sock';listener.bind(str(address))
        gateway=uvicorn.Server(uvicorn.Config(app,log_level='error'))
        thread=threading.Thread(target=gateway.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
        try:
            deadline=time.monotonic()+10
            while not gateway.started and time.monotonic()<deadline:time.sleep(.02)
            assert gateway.started
            rc=launch_claude(image,work,tmp_path/'trace',address,'fixture-token','c01',
                'Use Bash to write marker.txt, then finish.',timeout=60,effort='max',output_tokens=32768)
            assert rc==0,((tmp_path/'trace/claude.stdout').read_text(),
                [{k:v for k,v in body.items() if k in ('context_management','thinking','output_config')} for body in requests])
            assert (work/'marker.txt').read_text().strip()=='CLI_TOOL_OK'
            assert len(received)==2
            second=received[1]['body']['input']
            assert all(item in second for item in tool_answer()['output'])
            assert any(item.get('type')=='function_call_output' for item in second)
            assert app.state.ledger.status('development')[0]['calls']==2
        finally:
            gateway.should_exit=True;thread.join(timeout=5)
