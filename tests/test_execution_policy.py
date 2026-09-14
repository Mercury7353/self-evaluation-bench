import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from fastapi.testclient import TestClient

from seb.gateway import create_app
from seb.execution_policy import DEFAULT_POLICY, classify_failure
from seb.ledger import Ledger


def test_concurrency_backpressure_waits_without_spending_or_releasing_unknowns(tmp_path):
    import asyncio
    from seb.execution_policy import reserve_with_backpressure
    from seb.ledger import BudgetExceeded
    ledger=Ledger(tmp_path/'budget');ledger.wallet('w',.2)
    ledger.reserve('a','w','m',.15)
    async def run():
        async def settle():
            await asyncio.sleep(.03);ledger.finish('a',.02,{},'completed')
        task=asyncio.create_task(settle())
        await reserve_with_backpressure(ledger,'b','w','m',.15,1)
        await task
    asyncio.run(run())
    assert ledger.status('w')[0]['calls']==2
    ledger.finish('b',None,{},'transport_unknown')
    with pytest.raises(BudgetExceeded):
        asyncio.run(reserve_with_backpressure(ledger,'c','w','m',.05,1))
    assert ledger.status('w')[0]['calls']==2


@pytest.fixture
def provider(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            server.bodies.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            status, body, extra = server.responses.pop(0)
            self.send_response(status)
            for key, value in extra.items(): self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.bodies, server.responses = [], []
    Thread(target=server.serve_forever, daemon=True).start()
    key = tmp_path/'key'; key.write_text('PRIVATE_KEY')
    config = {'key_file':str(key), 'artifacts':str(tmp_path/'gateway'),
              'upstream':f'http://127.0.0.1:{server.server_port}',
              'evaluation_policy':DEFAULT_POLICY | {'retry_backoff_seconds':0},
              'prices':{'c01':{'input':1,'output':1}},
              'model_backends':{'c01':{'model':'secret-model'}},
              'tokens':{'token':{'wallet':'test','cap':10,'models':['c01'], 'trace_scopes':['f'*32]}}}
    yield server, config
    server.shutdown(); server.server_close()


def answer(text='ok', stop='end_turn', usage=True):
    body = {'model':'secret-model','content':[{'type':'text','text':text}], 'stop_reason':stop}
    if usage: body['usage']={'input_tokens':10,'output_tokens':10}
    return 200, json.dumps(body).encode(), {'Content-Type':'application/json'}


def post(client, **extra):
    return client.post('/anthropic/v1/messages',json={'model':'c01','max_tokens':32768,
                       'messages':[{'role':'user','content':'test'}], **extra},
                       headers={'x-api-key':'token','x-seb-operation-id':'fixed-call'})


def test_three_transient_retries_charge_each_attempt_and_replay_after_restart(provider):
    server, config = provider
    server.responses = [(503,b'{"error":{"type":"overloaded_error"}}',{})]*3 + [answer()]
    with TestClient(create_app(config)) as client:
        response=post(client)
        assert response.status_code==200 and response.headers['x-seb-attempt-count']=='4'
        assert response.json()['model']=='c01'
        assert [b['max_tokens'] for b in server.bodies]==[32768]*4
        assert {b['model'] for b in server.bodies}=={'secret-model'}
        assert post(client).headers['x-seb-replayed']=='true'
        assert post(client,max_tokens=65536).status_code==409
    with TestClient(create_app(config)) as client:
        assert post(client).headers['x-seb-replayed']=='true'
    assert len(server.bodies)==4
    ledger=Ledger(config['artifacts']+'/ledger.sqlite')
    row=ledger.status()[0]
    assert row['calls']==4 and row['charged']>0 and row['outstanding']>0 and row['cap']==10
    from pathlib import Path
    assert len(list((Path(config['artifacts'])/'scopes'/('f'*32)).iterdir()))==4
    for p in (Path(config['artifacts'])/'wire').rglob('*'):
        if p.is_file(): assert b'PRIVATE_KEY' not in p.read_bytes()


@pytest.mark.parametrize('response', [answer('wrong'),answer(''),answer('', 'max_tokens'),
    (400,b'{"error":{"type":"invalid_request_error"}}',{}),
    (500,json.dumps({'error':{'type':'api_error','message':json.dumps([{'error':{
        'code':400,'status':'INVALID_ARGUMENT','message':'Function call is missing a thought_signature'}}])}}).encode(),{}),
    (401,b'{"error":{"type":"authentication_error"}}',{}),
    (429,b'{"error":{"type":"insufficient_quota"}}',{})])
def test_noninfra_and_model_failures_never_retry(provider,response):
    server,config=provider;server.responses=[response]
    with TestClient(create_app(config)) as client: result=post(client)
    assert len(server.bodies)==1 and result.headers['x-seb-attempt-count']=='1'
    assert result.status_code==(200 if response[0]==200 else 422)


def test_exhaustion_is_terminal_and_partial_stream_is_not_released(provider):
    server,config=provider
    partial=b'data: {"type":"content_block_delta","delta":{"text":"discard this"}}\n\n'
    server.responses=[(200,partial,{'Content-Type':'text/event-stream'})]*4
    with TestClient(create_app(config)) as client: response=post(client,stream=True)
    assert len(server.bodies)==4 and response.status_code==422
    assert b'discard this' not in response.content
    assert Ledger(config['artifacts']+'/ledger.sqlite').status()[0]['outstanding']>0


def test_output_floor_rejected_before_api_or_charge(provider):
    server,config=provider
    with TestClient(create_app(config)) as client:
        for bad in (2048,16384,True,131073): assert post(client,max_tokens=bad).status_code==400
    assert not server.bodies
    assert Ledger(config['artifacts']+'/ledger.sqlite').status()[0]['calls']==0


@pytest.mark.parametrize('total,expected', [(260,.0022275),(999,None)])
def test_chat_reasoning_charge_or_unknown_persists_without_repeating(provider,total,expected):
    server,config=provider
    config['prices']['c01']={'input':1.5,'output':9}
    usage={'prompt_tokens':15,'completion_tokens':4,'total_tokens':total,
           'completion_tokens_details':{'reasoning_tokens':241}}
    payload={'model':'secret-model','choices':[{'message':{'content':'1591'},'finish_reason':'stop'}],
             'usage':usage}
    server.responses=[(200,json.dumps(payload).encode(),{'Content-Type':'application/json'})]
    with TestClient(create_app(config)) as client:
        for _ in range(2):
            response=client.post('/v1/openai/chat/completions',
                json={'model':'c01','max_tokens':32768,'messages':[{'role':'user','content':'probe'}]},
                headers={'x-api-key':'token','x-seb-operation-id':'chat-reasoning'})
            assert response.status_code==200
    assert len(server.bodies)==1
    with Ledger(config['artifacts']+'/ledger.sqlite').connect() as db:
        row=db.execute('SELECT * FROM calls').fetchone()
    assert json.loads(row['usage'])==usage
    if expected is None:
        assert row['charged'] is None and row['reserve']>0
    else:
        assert row['charged']==pytest.approx(expected)


@pytest.mark.parametrize('policy_enabled', [True, False])
@pytest.mark.parametrize('path', ['/anthropic/v1/messages',
                                 '/anthropic/v1/messages/count_tokens',
                                 '/v1/openai/chat/completions'])
def test_provider_routes_cannot_override_candidate_or_budget(provider, policy_enabled, path):
    server, config = provider
    if not policy_enabled:
        config.pop('evaluation_policy')
    body = {'model':'c01', 'max_tokens':32768, 'messages':[]}
    with TestClient(create_app(config)) as client:
        for extra in ({'models':['unlisted-expensive-model']},
                      {'models':['c01','unlisted-expensive-model']},
                      {'fallbacks':[{'model':'unlisted-expensive-model'}]},
                      {'fallbacks':'default'}, {'models':None}):
            result = client.post(path, json=body | extra, headers={'x-api-key':'token'})
            assert result.status_code == 400
            assert 'frozen model route' in result.json()['error']
    assert server.bodies == []
    assert Ledger(config['artifacts']+'/ledger.sqlite').status()[0]['calls'] == 0


@pytest.mark.parametrize('parameters', [
    {'model':'different-candidate'}, {'models':['unlisted-expensive-model']},
    {'fallbacks':'default'},
])
def test_frozen_parameters_cannot_inject_a_different_route(provider, parameters):
    server, config = provider
    config['frozen_request_parameters']={'c01':parameters}
    with TestClient(create_app(config)) as client:
        result=post(client)
        assert result.status_code == 400
    assert server.bodies == []
    assert Ledger(config['artifacts']+'/ledger.sqlite').status()[0]['calls'] == 0


def test_retry_stops_at_original_budget(provider):
    server,config=provider
    config['tokens']['token']['cap']=.05
    server.responses=[(503,b'{"error":{"type":"overloaded_error"}}',{})]
    with TestClient(create_app(config)) as client: response=post(client)
    assert response.status_code==402 and len(server.bodies)==1
    row=Ledger(config['artifacts']+'/ledger.sqlite').status()[0]
    assert row['cap']==.05 and row['calls']==1 and row['outstanding']>0


def test_incomplete_operation_never_restarts(provider):
    from pathlib import Path
    import hashlib
    server,config=provider
    folder=Path(config['artifacts'])/'operations'/hashlib.sha256(b'test\0fixed-call').hexdigest()
    folder.mkdir(parents=True)
    with TestClient(create_app(config)) as client: assert post(client).status_code==409
    assert not server.bodies


def test_transport_failure_allowlist():
    import httpx
    assert classify_failure(None,exception=httpx.ReadTimeout('timeout'))==('infra_error',True)
    assert classify_failure(None,exception=ValueError('bad local config'))==('internal_error',False)


def test_frozen_parameters_and_protocol_change_cannot_reuse_old_result(provider):
    server,config=provider
    config['frozen_request_parameters']={'c01':{'temperature':1}}
    server.responses=[answer()]
    with TestClient(create_app(config)) as client:
        assert post(client,temperature=0).status_code==400
        assert post(client).status_code==200
        assert server.bodies[0]['temperature']==1
    config['model_backends']['c01']['model']='different-checkpoint'
    with TestClient(create_app(config)) as client:assert post(client).status_code==409
    assert len(server.bodies)==1
