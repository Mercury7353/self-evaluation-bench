import asyncio
import copy
import json
import multiprocessing
import os
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient
import yaml

from seb.gateway import create_app
from seb.ledger import Ledger
from seb.response_cache import Lease, CacheError
from seb.research import scoped_cost, validate_result
from test_execution_policy import provider as anthropic_provider, answer
from test_responses_candidate import provider as native_provider, tool_answer, answer as native_answer, payload
from test_experiment import fixture_config, joint_fixture


def configuration(cfg, root, *, wallet='development'):
    cfg=copy.deepcopy(cfg)
    cfg['artifacts']=str(root/'gateway')
    cfg['response_cache']={'version':1,'directory':str(root.parent/'shared-responses'),'namespace':'frozen-fixture'}
    for e in cfg['tokens'].values():
        e['wallet']=wallet
        e['trace_scopes']=[('a' if root.name=='one' else 'b')*32]
        e['budget_scopes']={'item:'+root.name:10}
    return cfg


def call(cfg, *, operation='one', sample=None, body=None):
    headers={'x-api-key':next(iter(cfg['tokens'])),'x-seb-operation-id':operation}
    if sample is not None:headers['x-seb-sample-id']=sample
    with TestClient(create_app(cfg)) as client:
        return client.post('/anthropic/v1/messages',json=body or payload(),headers=headers)


def ledger(cfg):
    return Ledger(Path(cfg['artifacts'])/'ledger.sqlite')


def test_two_runs_pay_equal_quota_with_fresh_owned_evidence(anthropic_provider,tmp_path):
    server,base=anthropic_provider;server.responses=[answer('wrong')]
    one=configuration(base,tmp_path/'one');two=configuration(base,tmp_path/'two')
    first=call(one);started=time.time();second=call(two)
    assert first.status_code==second.status_code==200
    assert first.json()==second.json() and len(server.bodies)==1
    a,b=first.headers['x-seb-request-id'],second.headers['x-seb-request-id']
    assert a!=b
    wa,wb=ledger(one).status()[0],ledger(two).status()[0]
    assert wa['charged']==wb['charged']>0 and wb['response_cache_hits']==1
    assert wa['provider_metered_usd']>0 and wb['provider_metered_usd']==0
    # Replay belongs to the current execution and item scope, not its source run.
    validate_result({'score':0,'items':[{'id':'item','score':0,'evidence':[{'kind':'llm','id':b}]}]},
                    ledger(two),'development',started,tmp_path/'jobs')
    with ledger(two).connect() as db:
        assert db.execute('SELECT scope FROM call_budget_scopes WHERE call_id=?',(b,)).fetchone()[0]=='item:two'
        assert db.execute('SELECT source_call_id FROM cache_replays').fetchone()[0]==a
    costs=scoped_cost(two,'b'*32)
    assert costs['charged_usd']==wb['charged'] and costs['provider_metered_usd']==0
    assert costs['usage_by_model']=={} and costs['equivalent_usage_by_model']['c01']['output_tokens']==10
    assert call(two).headers['x-seb-replayed']=='true'
    assert ledger(two).status()[0]['calls']==1
    assert call(two,sample='new').status_code==409  # operation identity includes the sample
    assert len(server.bodies)==1


@pytest.mark.parametrize('text,stop',[('','end_turn'),('','max_tokens'),('refused','end_turn')])
def test_completed_empty_truncated_and_refused_answers_are_preserved(anthropic_provider,tmp_path,text,stop):
    server,base=anthropic_provider;server.responses=[answer(text,stop)]
    for name in ('one','two'):
        cfg=configuration(base,tmp_path/name);response=call(cfg)
        assert response.json()['content'][0]['text']==text and response.json()['stop_reason']==stop
    assert len(server.bodies)==1


@pytest.mark.parametrize('change',['sample','phase','price','model','namespace','policy','request'])
def test_distinct_request_protocol_or_sampling_never_reuses(anthropic_provider,tmp_path,change):
    server,base=anthropic_provider;server.responses=[answer(),answer()]
    one=configuration(base,tmp_path/'one');two=configuration(base,tmp_path/'two')
    assert call(one).status_code==200
    kwargs={}
    if change=='sample':kwargs['sample']='replicate-2'
    elif change=='phase':next(iter(two['tokens'].values()))['wallet']='evaluation'
    elif change=='price':two['prices']['c01']['output']=2
    elif change=='model':two['model_backends']['c01']['model']='other-model'
    elif change=='namespace':two['response_cache']['namespace']='new-campaign'
    elif change=='policy':two['evaluation_policy']['attempt_timeout_seconds']=901
    else:kwargs['body']=payload(messages=[{'role':'user','content':'different'}])
    assert call(two,**kwargs).status_code==200
    assert len(server.bodies)==2 and ledger(two).status()[0]['response_cache_hits']==0


def test_hit_requires_same_conservative_reservation_as_miss(anthropic_provider,tmp_path):
    server,base=anthropic_provider;server.responses=[answer()]
    assert call(configuration(base,tmp_path/'one')).status_code==200
    two=configuration(base,tmp_path/'two');next(iter(two['tokens'].values()))['cap']=.001
    assert call(two).status_code==402  # known answer price fits, original reserve does not
    assert len(server.bodies)==1 and ledger(two).status()[0]['calls']==0


@pytest.mark.parametrize('failure',[answer(usage=False),(403,b'{"error":{"type":"permission_error"}}',{})])
def test_unknown_or_failed_delivery_not_published(anthropic_provider,tmp_path,failure):
    server,base=anthropic_provider;server.responses=[failure,answer()]
    one=configuration(base,tmp_path/'one');two=configuration(base,tmp_path/'two')
    call(one);before=ledger(one).status()[0]
    assert not list((tmp_path/'shared-responses').glob('entries/*/*'))
    assert call(two).status_code==200 and len(server.bodies)==2
    assert ledger(one).status()[0]==before and before['outstanding']>0


def test_success_after_infra_retry_not_shared_without_prior_unknown_charges(anthropic_provider,tmp_path):
    server,base=anthropic_provider
    server.responses=[(503,b'{"error":{"type":"overloaded_error"}}',{}),answer(),answer()]
    one=configuration(base,tmp_path/'one');two=configuration(base,tmp_path/'two')
    assert call(one).headers['x-seb-attempt-count']=='2'
    assert not list((tmp_path/'shared-responses').glob('entries/*/*'))
    assert call(two).status_code==200 and len(server.bodies)==3
    assert ledger(one).status()[0]['outstanding']>0


def test_corrupt_entry_fails_without_paid_fallback(anthropic_provider,tmp_path):
    server,base=anthropic_provider;server.responses=[answer()]
    call(configuration(base,tmp_path/'one'))
    entry=next((tmp_path/'shared-responses').glob('entries/*/*/response.body'))
    entry.write_bytes(b'not the saved answer')
    two=configuration(base,tmp_path/'two');response=call(two)
    assert response.status_code==422 and response.json()['error']['type']=='cache_error'
    assert len(server.bodies)==1 and ledger(two).status()[0]['charged']==0
    assert ledger(two).status()[0]['outstanding']==0


def _process_call(cfg,ready,queue):
    try:
        ready.wait(10)
        r=call(cfg);queue.put((r.status_code,r.headers.get('x-seb-request-id')))
    except Exception as e:queue.put(('error',str(e)))


def test_separate_gateway_processes_share_only_one_provider_fill(anthropic_provider,tmp_path):
    server,base=anthropic_provider;server.responses=[answer()]
    ctx=multiprocessing.get_context('spawn');ready=ctx.Event();queue=ctx.Queue()
    configs=[configuration(base,tmp_path/name) for name in ('one','two')]
    children=[ctx.Process(target=_process_call,args=(cfg,ready,queue)) for cfg in configs]
    try:
        for child in children:child.start()
        ready.set()
        result=[queue.get(timeout=25) for _ in children]
        for child in children:child.join(10)
        assert all(c.exitcode==0 for c in children)
        assert [r[0] for r in result]==[200,200] and result[0][1]!=result[1][1]
        assert len(server.bodies)==1
        assert sum(ledger(c).status()[0]['response_cache_hits'] for c in configs)==1
    finally:
        for child in children:
            if child.is_alive():child.terminate();child.join(5)
        queue.close()


def test_lock_wait_deadline_and_cancellation_release_without_issuing_call(tmp_path):
    async def run():
        owner=Lease(tmp_path,'a'*64);waiter=Lease(tmp_path,'a'*64)
        try:
            await owner.acquire(time.time()+1)
            with pytest.raises(CacheError):await waiter.acquire(time.time()+.06)
            waiter.close()
            pending=asyncio.create_task(waiter.acquire(time.time()+5))
            await asyncio.sleep(.03);pending.cancel()
            with pytest.raises(asyncio.CancelledError):await pending
            waiter.close();owner.close()
            await waiter.acquire(time.time()+1)
        finally:owner.close();waiter.close()
    asyncio.run(run())


def test_native_tool_cache_rebuilds_opaque_history_in_new_token_namespace(native_provider,tmp_path):
    server,received,base=native_provider
    server.respond=lambda body:(200,tool_answer() if not any(x.get('type')=='function_call_output' for x in body['input']) else native_answer())
    configs=[configuration(base,tmp_path/name) for name in ('one','two')]
    results=[]
    for cfg in configs:
        first=call(cfg)
        assert first.status_code==200
        messages=payload()['messages']+[{'role':'assistant','content':first.json()['content']},
            {'role':'user','content':[{'type':'tool_result','tool_use_id':'call_test','content':'tool output'}]}]
        last=call(cfg,operation='tool-result',body=payload(messages=messages))
        assert last.status_code==200 and 'READY' in last.text
        results.append(last.json())
        saved=list((Path(cfg['artifacts'])/'responses-state').rglob('*.json'))
        assert saved and any('opaque-provider-state' in p.read_text() for p in saved)
    assert len(received)==2 and results[0]==results[1]
    assert ledger(configs[1]).status()[0]['response_cache_hits']==2
    # Provider-cache discounts and response-replay savings are different quantities.
    from seb.experiment import accounting
    cfg={'models':[{'id':'c01','price':base['prices']['c01']}],'researchers':[],
         'budgets':{'researcher_usd':1,'development_usd':25,'evaluation_usd':25}}
    billed=accounting(Path(configs[1]['artifacts']).parent,cfg)
    assert billed['by_model'][0]['metered_usd']>0
    assert billed['by_model'][0]['cache_adjusted_estimate_usd']==0
    assert billed['by_model'][0]['provider_metered_usd']==0


def test_cache_config_rejects_mounted_paths_and_keeps_private_namespace_out_of_prompt(tmp_path):
    from seb.experiment_config import load
    from seb.experiment import build_gateway,prepare_workspace
    path,cfg=fixture_config(tmp_path)
    cfg['response_cache']={'version':1,'directory':str(tmp_path/'rootfs/cache'),'namespace':'private-id'}
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError,match='separate'):load(path)
    cfg['response_cache']['directory']='shared-cache'
    path.write_text(yaml.safe_dump(cfg));loaded=load(path)
    assert loaded['response_cache']['directory']==str(tmp_path/'shared-cache')
    out=tmp_path/'run';out.mkdir()
    config,tokens=build_gateway(loaded,loaded['researchers'][0],out,tmp_path/'sock',mock_url='http://127.0.0.1:9')
    work=prepare_workspace(loaded,config,tokens,out)
    text='\n'.join((work/name).read_text() for name in ('access.json','CONTRACT.md','whitebox.json'))
    assert 'private-id' not in text and 'shared-cache' not in text
    assert 'equivalent_test_charge' in text


def test_joint_full_pipeline_reuses_across_research_runs_without_reusing_evidence(tmp_path,monkeypatch):
    root=os.environ.get('SEB_TEST_ROOT');science=os.environ.get('SEB_TEST_SCIENCE')
    if not root or not science:pytest.skip('Set sandbox paths for joint integration')
    import seb.experiment as experiment
    from types import SimpleNamespace
    path,cfg=joint_fixture(tmp_path)
    cfg['runtime']={'rootfs':root,'science_packages':science}
    cfg['response_cache']={'version':1,'directory':str(tmp_path/'cache'),'namespace':'joint-fixture'}
    original=experiment.mock_submission
    def submission(work):
        access=(work/'access.json').read_text()
        assert str(tmp_path/'cache') not in access and 'joint-fixture' not in access
        original(work)
        file=work/'submission/evaluation.json';manifest=json.loads(file.read_text())
        manifest['domain_aggregations']={d:{'kind':'weighted_mean','weights':{'sum':1,'product':1}}
                                         for d in cfg['domain_protocol']['domains']}
        file.write_text(json.dumps(manifest))
        (work/'submission/predictor.py').write_text('''
def fit(training_rows,target_metadata):
    assert len(target_metadata)==6 and all('whitebox' in t for t in target_metadata)
    return list(target_metadata)
def predict(fitted,observations):
    return {t:sum(observations.values())/len(observations) for t in fitted}
''')
    server=experiment.mock_provider();received=[]
    handle=server.RequestHandlerClass.do_POST
    def counted(self):
        received.append(self.path);return handle(self)
    monkeypatch.setattr(server.RequestHandlerClass,'do_POST',counted)
    monkeypatch.setattr(experiment,'mock_provider',lambda:SimpleNamespace(server_port=server.server_port,
        shutdown=lambda:None,server_close=lambda:None))
    monkeypatch.setattr(experiment,'mock_submission',submission)
    path.write_text(yaml.safe_dump(cfg))
    outputs=[tmp_path/'joint-first',tmp_path/'joint-second'];results=[]
    try:
        for out in outputs:
            result=experiment.run(path,None,out,mock=True);results.append(result)
            assert result['eligible'] and result['expected_models']==3
            assert len(json.loads((out/'acceptance-jobs/results.json').read_text()))==3
            scores=json.loads((out/'domain/scores.json').read_text())
            assert len(scores['visible'])==len(scores['sealed'])==6
        assert len(received)==12  # Six development + six acceptance; second researcher costs zero provider calls.
        ledgers=[Ledger(out/'gateway/ledger.sqlite') for out in outputs]
        before,after=[{w['name']:w for w in led.status()} for led in ledgers]
        for wallet in ('development','evaluation'):
            assert before[wallet]['charged']==after[wallet]['charged']>0
            assert after[wallet]['calls']==after[wallet]['response_cache_hits']==6
            assert after[wallet]['provider_metered_usd']==0
        with ledgers[0].connect() as a,ledgers[1].connect() as b:
            first_ids={r[0] for r in a.execute('SELECT id FROM calls')}
            second_ids={r[0] for r in b.execute('SELECT id FROM calls')}
            assert first_ids.isdisjoint(second_ids)
            assert {r[0] for r in b.execute('SELECT source_call_id FROM cache_replays')}==first_ids
        for out in outputs:
            assert not (out/'researcher-work/access.json').exists()  # scoped credentials removed at shutdown
            assert 'blackbox' not in (out/'domain/visible-prediction/input.json').read_text()
    finally:server.shutdown();server.server_close()


def test_designer_wallet_is_never_a_shared_response_cache(anthropic_provider,tmp_path):
    server,base=anthropic_provider;server.responses=[answer(),answer()]
    for name in ('one','two'):
        cfg=configuration(base,tmp_path/name,wallet='designer')
        assert call(cfg,body=payload(max_tokens=1024)).status_code==200
        assert ledger(cfg).status()[0]['response_cache_hits']==0
    assert len(server.bodies)==2 and not (tmp_path/'shared-responses').exists()


def test_streaming_response_is_replayed_with_exact_original_usage(anthropic_provider,tmp_path):
    server,base=anthropic_provider
    events=[{'type':'message_start','message':{'model':'secret-model','usage':{'input_tokens':12,'output_tokens':0}}},
            {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'wrong'}},
            {'type':'message_delta','usage':{'output_tokens':2},'delta':{'stop_reason':'end_turn'}},
            {'type':'message_stop'}]
    raw=''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode()
    server.responses=[(200,raw,{'Content-Type':'text/event-stream'})]
    responses=[]
    for name in ('one','two'):
        cfg=configuration(base,tmp_path/name)
        responses.append(call(cfg,body=payload(stream=True)))
    assert all(r.status_code==200 for r in responses) and responses[0].content==responses[1].content
    assert len(server.bodies)==1 and ledger(cfg).status()[0]['response_cache_hits']==1


def test_gateway_cache_wait_leaves_old_unknown_cost_untouched(anthropic_provider,tmp_path):
    server,base=anthropic_provider;server.responses=[answer()]
    one=configuration(base,tmp_path/'one');two=configuration(base,tmp_path/'two')
    first=call(one)
    meta=json.loads((Path(one['artifacts'])/'wire'/first.headers['x-seb-request-id']/'meta.json').read_text())
    owner=Lease(Path(one['response_cache']['directory']),meta['response_cache_key'])
    asyncio.run(owner.acquire(time.time()+2))
    db=ledger(two);db.wallet('development',10);db.reserve('old-unknown','development','c01',.2)
    db.finish('old-unknown',None,{},'transport_unknown')
    with db.connect() as conn:before=dict(conn.execute('SELECT * FROM calls WHERE id=?',('old-unknown',)).fetchone())
    next(iter(two['tokens'].values()))['deadline_epoch']=time.time()+.3
    try:response=call(two)
    finally:owner.close()
    assert response.status_code==422 and response.json()['error']['type']=='cache_error'
    assert len(server.bodies)==1 and db.status()[0]['charged']==0
    assert db.status()[0]['outstanding']==.2
    with db.connect() as conn:
        assert dict(conn.execute('SELECT * FROM calls WHERE id=?',('old-unknown',)).fetchone())==before
        assert conn.execute('SELECT charged FROM calls WHERE id!=?',('old-unknown',)).fetchone()[0]==0
