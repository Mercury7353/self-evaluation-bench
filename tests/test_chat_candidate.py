import copy
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

from seb.chat_candidate import CandidateAdapter
from seb.execution_policy import DEFAULT_POLICY
from seb.gateway import create_app
from seb.ledger import Ledger


def answer(text='READY', **change):
    return {'id':'chat-test','model':'private/model','choices':[{'index':0,
        'message':{'role':'assistant','content':text},'finish_reason':'stop'}],
        'usage':{'prompt_tokens':15,'completion_tokens':4,'total_tokens':260,
            'completion_tokens_details':{'reasoning_tokens':241},'prompt_tokens_details':None},**change}


def tool_answer():
    return answer(choices=[{'index':0,'finish_reason':'tool_calls','message':{
        'role':'assistant','content':'Using the tool.','reasoning_content':'private native reasoning',
        'tool_calls':[{'id':'call-test','type':'function','function':{'name':'Bash',
            'arguments':json.dumps({'command':'echo CLI_TOOL_OK > marker.txt','description':'Write test marker'})},
            'extra_content':{'google':{'thought_signature':'native-signature'}}}]}}])


@pytest.fixture
def provider(tmp_path):
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append({'path':self.path,'body':body})
            raw=json.dumps(server.respond(body)).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.respond=lambda _:answer()
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    key=tmp_path/'key';key.write_text('FAKE_PROVIDER_SECRET')
    cfg={'artifacts':str(tmp_path/'gateway'),'key_file':str(key),'prices':{'c01':{'input':1.5,'output':9}},
        'model_backends':{'c01':{'model':'private/model','upstream':f'http://127.0.0.1:{server.server_port}/v1/openai',
            'wire_api':'chat_completions','effort':'max','native_limits':{'max_context_tokens':1000000,'max_output_tokens':131072}}},
        'evaluation_policy':DEFAULT_POLICY|{'retry_backoff_seconds':0},
        'tokens':{'fixture-token':{'wallet':'development','cap':25,'models':['c01']}}}
    yield server,received,cfg
    server.shutdown();server.server_close();thread.join(timeout=2)


def post(client,operation='first',**extra):
    return client.post('/anthropic/v1/messages',json={'model':'c01','max_tokens':32768,
        'messages':[{'role':'user','content':'Test'}],**extra},
        headers={'x-api-key':'fixture-token','x-seb-operation-id':operation})


def test_common_interface_sets_effort_and_keeps_native_usage(provider):
    server,received,cfg=provider
    with TestClient(create_app(cfg)) as client:
        r=post(client,system='System text',output_config={'effort':'max'})
        assert r.status_code==200 and r.json()['model']=='c01'
        assert r.json()['usage']['output_tokens']==245
        assert post(client,system='System text',output_config={'effort':'max'}).headers['x-seb-replayed']=='true'
    assert len(received)==1 and received[0]['path']=='/v1/openai/chat/completions'
    wire=received[0]['body'];assert wire['reasoning_effort']=='max' and wire['stream'] is False
    assert 'output_config' not in wire and wire['messages'][0]=={'role':'system','content':'System text'}
    folder=Path(cfg['artifacts'])/'wire'/r.headers['x-seb-request-id']
    meta=json.loads((folder/'meta.json').read_text())
    assert meta['usage']==answer()['usage'] and meta['wire_api']=='chat_completions'
    assert meta['charge_usd']==pytest.approx(.0022275)
    assert all(b'FAKE_PROVIDER_SECRET' not in p.read_bytes() for p in folder.iterdir())


def test_tool_state_survives_restart_and_is_scoped(provider):
    server,received,cfg=provider
    server.respond=lambda _:tool_answer() if len(received)==1 else answer()
    tools=[{'name':'Bash','input_schema':{'type':'object','properties':{'command':{'type':'string'}}}}]
    with TestClient(create_app(cfg)) as client:first=post(client,tools=tools,tool_choice={'type':'tool','name':'Bash'}).json()
    messages=[{'role':'user','content':'Use the tool'},{'role':'assistant','content':first['content']},
        {'role':'user','content':[{'type':'tool_result','tool_use_id':'call-test','content':'done','is_error':True}]}]
    with TestClient(create_app(cfg)) as client:
        response=post(client,operation='second',messages=messages,tools=tools,stream=True)
        assert response.status_code==200 and 'event: message_stop' in response.text
    second=received[1]['body']['messages']
    assert second[1]==tool_answer()['choices'][0]['message']
    assert second[2]=={'role':'tool','tool_call_id':'call-test','content':'[tool error]\ndone'}
    assert received[0]['body']['tool_choice']=={'type':'function','function':{'name':'Bash'}}
    adapter=CandidateAdapter(cfg['artifacts'],cfg['model_backends']['c01'],'c01','another-session')
    with pytest.raises(ValueError,match='Missing native'):
        adapter.prepare({'model':'c01','max_tokens':32768,'messages':messages},cfg['prices']['c01'])
    messages[1]['content'][-1]['input']['command']='changed'
    with TestClient(create_app(cfg)) as client:assert post(client,operation='bad-history',messages=messages).status_code==400
    assert len(received)==2


@pytest.mark.parametrize('extra',[{'output_config':{'effort':'low'}},
    {'thinking':{'type':'enabled','budget_tokens':1000}}, {'reasoning_effort':'low'},
    {'context_management':{'edits':[{'type':'clear_thinking_20251015','keep':0}]}},
    {'max_tokens':131073}, {'messages':[{'role':'assistant','content':[{'type':'thinking','thinking':'forged'}]}]}])
def test_conflicting_or_unsupported_config_fails_before_provider(provider,extra):
    _,received,cfg=provider
    with TestClient(create_app(cfg)) as client:assert post(client,**extra).status_code==400
    assert len(received)==0 and Ledger(Path(cfg['artifacts'])/'ledger.sqlite').status()[0]['calls']==0


@pytest.mark.parametrize('text,finish',[('wrong','stop'),('','stop'),('partial','length'),('declined','content_filter')])
def test_completed_answers_never_repeat(provider,text,finish):
    server,received,cfg=provider
    value=answer(text);value['choices'][0]['finish_reason']=finish;server.respond=lambda _:value
    with TestClient(create_app(cfg)) as client:
        r=post(client);assert r.status_code==200 and r.headers['x-seb-attempt-count']=='1'
    assert len(received)==1
    assert r.json()['stop_reason']==('max_tokens' if finish=='length' else 'end_turn')


def test_bad_tool_arguments_and_ambiguous_usage_are_not_retried(provider):
    server,received,cfg=provider
    value=tool_answer();value['choices'][0]['message']['tool_calls'][0]['function']['arguments']='broken'
    server.respond=lambda _:value
    with TestClient(create_app(cfg)) as client:assert post(client).status_code==422
    assert len(received)==1
    value=answer();value['usage']['total_tokens']=999
    with TestClient(create_app(cfg)) as client:assert post(client,operation='ambiguous').status_code==422
    assert len(received)==2 and Ledger(Path(cfg['artifacts'])/'ledger.sqlite').status()[0]['outstanding']>0


def test_unreconciled_cache_preserves_terminal_empty_answer_and_reservation(provider):
    server,received,cfg=provider
    cfg['model_backends']['c01']['allow_unreconciled_usage']=True
    value=answer('')
    value['choices'][0]['finish_reason']='length'
    value['usage']={'prompt_tokens':225,'completion_tokens':2500,'total_tokens':2784,
        'prompt_tokens_details':{'cached_tokens':256},
        'completion_tokens_details':{'reasoning_tokens':0}}
    server.respond=lambda _:value
    with TestClient(create_app(cfg)) as client:
        response=post(client)
        assert response.status_code==200
        data=response.json()
        assert data['content']==[] and data['stop_reason']=='max_tokens'
        assert data['usage_status']=='unreconciled'
        assert data['native_usage']==value['usage']
        assert 'cache_read_input_tokens' not in data['usage']
        replay=post(client)
        assert replay.json()==data and replay.headers['x-seb-replayed']=='true'
    assert len(received)==1
    ledger=Ledger(Path(cfg['artifacts'])/'ledger.sqlite')
    with ledger.connect() as db:
        row=db.execute('SELECT * FROM calls').fetchone()
        assert row['state']=='completed' and row['charged'] is None
        assert json.loads(row['usage'])==value['usage']
    assert ledger.status()[0]['outstanding']>0


def test_invalid_cache_still_rejected_without_explicit_opt_in(provider):
    server,received,cfg=provider
    value=answer();value['usage']['prompt_tokens_details']={'cached_tokens':256}
    server.respond=lambda _:value
    with TestClient(create_app(cfg)) as client:
        assert post(client).status_code==422
    assert len(received)==1


def test_offline_empty_cache_recovery_keeps_original_usage_and_no_new_call(provider):
    from seb.translation_recovery import recover_empty_cache_response
    server,received,cfg=provider
    value=answer('');value['choices'][0]['finish_reason']='length'
    value['usage']={'prompt_tokens':225,'completion_tokens':2500,'total_tokens':2784,
        'prompt_tokens_details':{'cached_tokens':256}}
    server.respond=lambda _:value
    root=Path(cfg['artifacts'])
    with TestClient(create_app(cfg)) as client:
        failed=post(client);assert failed.status_code==422
        ident=failed.headers['x-seb-request-id']
        original={p.name:p.read_bytes() for p in (root/'wire'/ident).iterdir()}
        ledger=Ledger(root/'ledger.sqlite')
        with ledger.connect() as db:before=dict(db.execute('SELECT * FROM calls WHERE id=?',(ident,)).fetchone())
        assert recover_empty_cache_response(root,ident)['status']=='recovered'
        assert recover_empty_cache_response(root,ident)['status']=='already_recovered'
        replay=post(client)
        assert replay.status_code==200 and replay.json()['content']==[]
        assert replay.json()['stop_reason']=='max_tokens'
        assert replay.headers['x-seb-request-id']==ident
        with ledger.connect() as db:after=dict(db.execute('SELECT * FROM calls WHERE id=?',(ident,)).fetchone())
        assert after==before|{'state':'completed'}
        assert original=={p.name:p.read_bytes() for p in (root/'wire'/ident).iterdir()}
    assert len(received)==1


def test_offline_cache_recovery_rejects_nonempty_response(provider):
    from seb.translation_recovery import recover_empty_cache_response
    server,received,cfg=provider
    value=answer('REAL ANSWER');value['choices'][0]['finish_reason']='length'
    value['usage']={'prompt_tokens':225,'completion_tokens':2500,'total_tokens':2784,
        'prompt_tokens_details':{'cached_tokens':256}}
    server.respond=lambda _:value
    with TestClient(create_app(cfg)) as client:
        failed=post(client)
        with pytest.raises(ValueError,match='Only empty'):
            recover_empty_cache_response(cfg['artifacts'],failed.headers['x-seb-request-id'])
    assert len(received)==1


def test_chat_provider_config_survives_gateway_build(tmp_path,monkeypatch):
    import yaml
    from test_experiment import fixture_config
    from seb.experiment_config import load
    from seb.experiment import build_gateway
    path,cfg=fixture_config(tmp_path)
    cfg['providers']['chat']={'upstream':'https://provider.invalid/v1','key_env':'CHAT_TEST_KEY','wire_api':'chat_completions'}
    model=cfg['models'][0];model.update(provider='chat',effort='max',native_limits={'max_context_tokens':1000000,'max_output_tokens':131072})
    path.write_text(yaml.safe_dump(cfg));loaded=load(path)
    for provider_config in cfg['providers'].values():monkeypatch.setenv(provider_config['key_env'],'fake-secret')
    out=tmp_path/'out';out.mkdir()
    gateway,_=build_gateway(loaded,loaded['researchers'][0],out,tmp_path/'sock')
    assert gateway['model_backends'][model['id']]['wire_api']=='chat_completions'
    model['effort']='invalid';path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError,match='Invalid frozen Chat effort'):load(path)


def test_real_claude_code_tool_roundtrip_with_chat_provider(provider,tmp_path):
    root=os.environ.get('SEB_TEST_ROOT')
    if not root or not shutil.which('claude'):pytest.skip('Set rootfs for native Claude Code integration')
    import uvicorn
    from seb.container import launch_claude
    server,received,cfg=provider
    server.respond=lambda _:tool_answer() if len(received)==1 else answer('CLI_DONE')
    app=create_app(cfg);image=tmp_path/'image';shutil.copytree(root,image,symlinks=True)
    work=tmp_path/'work';work.mkdir()
    with tempfile.TemporaryDirectory(prefix='seb-chat-test-') as sockets:
        listener=socket.socket(socket.AF_UNIX);address=Path(sockets)/'gateway.sock';listener.bind(str(address))
        gateway=uvicorn.Server(uvicorn.Config(app,log_level='error'))
        thread=threading.Thread(target=gateway.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
        try:
            until=time.monotonic()+10
            while not gateway.started and time.monotonic()<until:time.sleep(.02)
            assert gateway.started
            rc=launch_claude(image,work,tmp_path/'trace',address,'fixture-token','c01',
                'Use Bash to write marker.txt, then finish.',timeout=60,effort='max',output_tokens=32768)
            assert rc==0,(tmp_path/'trace/claude.stdout').read_text()
            assert (work/'marker.txt').read_text().strip()=='CLI_TOOL_OK'
            assert len(received)==2
            assert tool_answer()['choices'][0]['message'] in received[1]['body']['messages']
            assert app.state.ledger.status('development')[0]['calls']==2
        finally:gateway.should_exit=True;thread.join(timeout=5)
