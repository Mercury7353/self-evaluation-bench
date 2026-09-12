import json
import time

import pytest
from fastapi.testclient import TestClient

from seb.evaluation import normalize_result, attach_accounting, load_manifest
from seb.execution_policy import DEFAULT_POLICY
from seb.ledger import Ledger
from seb.research import research_app


@pytest.fixture
def case(tmp_path):
    ledger=Ledger(tmp_path/'ledger');ledger.wallet('w',1)
    started=time.time()-1;ident='a'*32
    ledger.reserve(ident,'w','c01',.1)
    ledger.finish(ident,None,{},'completed')  # Valid delivery, missing billing usage.
    manifest={'protocol_version':1,'aggregation':{'kind':'weighted_mean'},
              'items':[{'id':'a'},{'id':'b'}]}
    items=[{'id':name,'score':score,'execution_status':'completed','answer_status':answer,
            'evidence':[{'kind':'llm','id':ident}]} for name,score,answer in [('a',1,'answered'),('b',0,'missing')]]
    return manifest,{'protocol_version':1,'score':.5,'items':items},ledger,started,tmp_path


def normalize(case):
    manifest,result,ledger,started,root=case
    return normalize_result(result,manifest,ledger,'w',started,root)


def test_valid_score_survives_unknown_cost_and_missing_answer_counts_zero(case):
    result=normalize(case)
    assert result['score']==.5 and result['score_status']=='valid'
    state=attach_accounting({'result':result},{'cost_complete':False,'outstanding_reserved_usd':.1})
    assert state['accounting_status']=='pending' and state['result']['score_status']=='valid'


def test_dropping_failures_or_raising_score_is_rejected(case):
    case[1]['score']=1
    with pytest.raises(ValueError,match='denominator'):normalize(case)
    case[1]['items'].pop()
    with pytest.raises(ValueError,match='exactly once'):normalize(case)


def test_infra_cannot_turn_into_a_ranked_score(case):
    case[1]['items'][1].update(execution_status='infra_error',answer_status='not_applicable',score=None)
    result=normalize(case)
    assert result['score'] is None and result['score_status']=='incomplete'
    assert result['weighted_score_lower_bound']==.5


def test_cross_wallet_evidence_and_false_missing_credit_rejected(case):
    case[1]['items'][1]['score']=1
    with pytest.raises(ValueError,match='receive zero'):normalize(case)
    case[1]['items'][1]['score']=0
    case[2].wallet('other',1)
    with pytest.raises(ValueError,match='outside'):normalize_result(case[1],case[0],case[2],'other',case[3],case[4])


def test_adaptive_exclusion_requires_frozen_rule(case):
    case[1]['items'][1].update(execution_status='not_selected',answer_status='not_applicable',score=None,evidence=[])
    with pytest.raises(ValueError,match='adaptive'):normalize(case)
    case[0]['selection']='adaptive';case[1]['score']=1
    result=normalize(case)
    assert result['score']==1 and result['selected_items']==1 and result['declared_items']==2


def test_manifest_preserves_hundred_item_contract(tmp_path):
    p=tmp_path/'evaluation.json'
    data={'protocol_version':1,'aggregation':{'kind':'weighted_mean'},'items':[{'id':str(i)} for i in range(100)]}
    p.write_text(json.dumps(data));assert len(load_manifest(tmp_path)['items'])==100
    data['items'].pop();p.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='at least 100'):load_manifest(tmp_path)


def test_researcher_info_does_not_expose_hidden_pool_prices_or_targets(tmp_path):
    key=tmp_path/'key';key.write_text('secret')
    config={'key_file':str(key),'artifacts':str(tmp_path/'gateway'),'evaluation_policy':DEFAULT_POLICY,
            'prices':{'c01':{'input':1,'output':1},'hidden':{'input':10,'output':50}},
            'model_backends':{'c01':{'model':'secret-checkpoint'}},
            'efforts':{'c01':'max','hidden':'high'},'hidden_targets':{'swe':90},
            'development_reference':[{'model':'c01','target_score':30},{'model':'hidden','target_score':90}],
            'tokens':{'token':{'wallet':'dev','cap':1,'models':['c01'],'research':True}}}
    with TestClient(research_app(config)) as client:
        response=client.get('/research/info',headers={'x-api-key':'token'})
    text=response.text
    assert response.status_code==200
    assert 'secret-checkpoint' not in text and 'hidden' not in text and 'prices' not in text and '"swe"' not in text
    assert response.json()['contract']['evaluation_policy']['min_output_tokens']==32768


def test_run_suite_keeps_scored_delivery_with_unsettled_cost(tmp_path,monkeypatch):
    from seb import research
    source=tmp_path/'submission';source.mkdir()
    (source/'run.py').write_text('pass')
    (source/'README.md').write_text('fixture')
    (source/'evaluation.json').write_text(json.dumps({'protocol_version':1,'aggregation':{'kind':'weighted_mean'},
        'items':[{'id':str(i)} for i in range(100)]}))
    base=tmp_path/'base';base.mkdir()
    config={'artifacts':str(tmp_path/'gateway'),'base_root':str(base),'gateway_socket':'unused',
            'evaluation_policy':DEFAULT_POLICY,'tokens':{}}
    ledger=Ledger(tmp_path/'gateway/ledger.sqlite');ledger.wallet('w',1)
    def run(command,log,**kwargs):
        execution=log.parent
        scope=next(iter(config['tokens'].values()))['trace_scopes'][-1]
        ident='b'*32;ledger.reserve(ident,'w','c01',.1);ledger.finish(ident,None,{},'completed')
        index=tmp_path/'gateway/scopes'/scope;index.mkdir(parents=True);(index/ident).touch()
        items=[{'id':str(i),'score':1,'execution_status':'completed','answer_status':'answered',
                'evidence':[{'kind':'llm','id':ident}]} for i in range(100)]
        (execution/'workspace/result.json').write_text(json.dumps({'protocol_version':1,'score':1,'items':items}))
        return 0
    monkeypatch.setattr(research,'contained',lambda *args,**kwargs:[])
    monkeypatch.setattr(research,'run_logged',run)
    result=research.run_suite(source,'c01','token',config,tmp_path/'execution',{'wallet':'w','cap':1,'models':['c01']})
    assert result['status']=='ok' and result['score_status']=='valid' and result['result']['score']==1
    assert result['accounting_status']=='pending' and result['cost']['outstanding_reserved_usd']==.1


def test_sdk_supplies_32k_and_does_not_retry_missing_answers(tmp_path,monkeypatch):
    from seb.research_sdk import Client
    ctx=tmp_path/'context.json'
    ctx.write_text(json.dumps({'token':'t','model':'c01','protocol_version':1,
        'evaluation_policy':DEFAULT_POLICY,'output_dir':str(tmp_path/'out')}))
    client=Client(ctx);calls=[]
    def request(path,data=None,**kwargs):
        calls.append(data)
        return b'{"content":[],"stop_reason":"max_tokens"}',{'x-seb-request-id':'a'*32,'x-seb-attempt-count':'1'}
    monkeypatch.setattr(client,'request',request)
    def grade(text):raise AssertionError('Missing answer should not be graded')
    item=client.item('one','question',grade)
    assert len(calls)==1 and calls[0]['max_tokens']==32768
    assert item['score']==0 and item['answer_status']=='missing' and item['finish_reason']=='max_tokens'
    with pytest.raises(ValueError):client.chat('question',max_tokens=2048)
    assert len(calls)==1


def test_queue_reports_running_and_only_queued_jobs_can_cancel(tmp_path,monkeypatch):
    import threading
    from seb import research
    source=tmp_path/'suite';source.mkdir()
    (source/'run.py').write_text('pass');(source/'README.md').write_text('fixture')
    key=tmp_path/'key';key.write_text('secret')
    config={'key_file':str(key),'artifacts':str(tmp_path/'gateway'),'prices':{'c01':{'input':1,'output':1}},
            'tokens':{'token':{'wallet':'w','cap':1,'models':['c01'],'research':True,'workspace':str(tmp_path)},
                      'other':{'wallet':'other','cap':1,'models':['c01']}}}
    started=threading.Event();release=threading.Event();calls=[]
    def run(*args):
        calls.append(1);started.set();release.wait(10);return {'status':'ok'}
    monkeypatch.setattr(research,'run_suite',run)
    with TestClient(research_app(config)) as client:
        headers={'x-api-key':'token'}
        try:
            first=client.post('/research/suites',headers=headers,json={'path':'suite','model':'c01'}).json()['id']
            assert started.wait(2)
            assert client.get('/research/jobs/'+first,headers=headers).json()['status']=='running'
            second=client.post('/research/suites',headers=headers,json={'path':'suite','model':'c01'}).json()['id']
            route='/research/jobs/'+second+'/cancel'
            assert client.post(route,headers={'x-api-key':'other'}).status_code==403
            assert client.post('/research/jobs/'+first+'/cancel',headers=headers).status_code==409
            assert client.post(route,headers=headers).json()['status']=='cancelled'
        finally:release.set()
    assert len(calls)==1
