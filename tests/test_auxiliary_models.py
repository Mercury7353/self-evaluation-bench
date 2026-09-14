import hashlib
import json
import os
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient
import yaml

from seb.evaluation import normalize_result, public_contract
from seb.evidence import check_budget_evidence
from seb.experiment import accounting, build_gateway, prepare_workspace
from seb.experiment_config import load, researcher_view
from seb.ledger import Ledger
from seb.research import research_app
from seb.research_sdk import Client, EvaluationError
from test_experiment import fixture_config
from test_execution_policy import provider as anthropic_provider, answer
from test_responses_candidate import provider as native_provider


def helper_config(tmp_path):
    path,cfg=fixture_config(tmp_path)
    cfg['auxiliary_models']=[{'id':'grader-01','roles':['grader'],'provider':'inference',
        'model':'private-grader-backend','effort':'high',
        'price':{'input':2,'output':3,'cache_read_multiplier':.1}},
        {'id':'simulator-01','roles':['simulator'],'provider':'inference',
         'model':'private-simulator-backend','price':{'input':1,'output':1}}]
    path.write_text(yaml.safe_dump(cfg))
    return path,cfg


@pytest.mark.parametrize('mutation',['id_collision','heldout_alias','missing_roles','bad_role','panel_split','bad_price','missing_native_limits'])
def test_invalid_helpers_fail_before_spending(tmp_path,mutation):
    path,cfg=helper_config(tmp_path);helper=cfg['auxiliary_models'][0]
    if mutation=='id_collision':helper['id']=cfg['models'][0]['id']
    elif mutation=='heldout_alias':helper['model']=cfg['models'][3]['model']
    elif mutation=='missing_roles':helper.pop('roles')
    elif mutation=='bad_role':helper['roles']=['candidate']
    elif mutation=='panel_split':helper['split']='development'
    elif mutation=='bad_price':helper['price']['output']=-1
    else:
        cfg['providers']['helper-native']={'upstream':'https://private.invalid/v1',
            'key_env':'PRIVATE_KEY','wire_api':'openai_responses'}
        helper['provider']='helper-native'
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError):load(path)


def test_helper_info_preserves_candidate_panel_and_private_boundary(tmp_path):
    path,_=helper_config(tmp_path);cfg=load(path);out=tmp_path/'run';out.mkdir()
    config,tokens=build_gateway(cfg,cfg['researchers'][0],out,tmp_path/'sock',mock_url='http://127.0.0.1:9')
    work=prepare_workspace(cfg,config,tokens,out)
    view=researcher_view(cfg)
    assert view['models']==['dev-a','dev-b','dev-c']
    assert len(view['auxiliary_models'])==2
    entry=config['tokens'][tokens['development']]
    assert set(entry['models'])==set(view['models'])|{'grader-01','simulator-01'}
    assert config['tokens'][tokens['designer']]['models']==['researcher-a']
    text='\n'.join((work/name).read_text() for name in ('whitebox.json','access.json'))
    assert all(s not in text for s in ('holdout','hidden-target','private-grader-backend','private-simulator-backend','upstream','key_env'))
    assert json.loads((work/'access.json').read_text())['efforts']['grader-01']=='high'
    with TestClient(research_app(config)) as client:
        headers={'x-api-key':tokens['development']}
        info=client.get('/research/info',headers=headers).json()
        assert info['models']==view['models']
        assert info['contract']['auxiliary_models']==view['auxiliary_models']
        for kind in ('suites','agents'):
            response=client.post('/research/'+kind,json={'model':'grader-01','path':'unused'},headers=headers)
            assert response.status_code==400
        assert not list((out/'research-jobs').iterdir())
        assert all(w['calls']==0 for w in client.get('/budget',headers=headers).json())


def connect_sdk(tmp_path,client,config,token):
    entry=config['tokens'][token]
    context=tmp_path/'client-context.json'
    context.write_text(json.dumps({'token':token,'model':'c01','output_dir':str(tmp_path/'sdk-raw'),
        **public_contract(config,entry)}))
    sdk=Client(context)
    def request(path,data=None,**kwargs):
        headers={'x-api-key':token}
        for key in ('operation_id','item_id','sample_id'):
            if kwargs.get(key) is not None:headers['x-seb-'+key.replace('_','-')]=kwargs[key]
        result=client.request('POST' if data is not None else 'GET',path,json=data,headers=headers)
        if result.status_code>=400:
            raise EvaluationError(result.status_code,result.json(),dict(result.headers))
        return result.content,dict(result.headers)
    sdk.request=request
    return sdk


def suite_access(config):
    scope='e'*32
    config['auxiliary_models']=['grader-01']
    config['prices']['grader-01']={'input':2,'output':3,'cache_read_multiplier':.1}
    config['model_backends']['grader-01']={'model':'private-grader'}
    config.update(require_item_budgets=True,item_cost_cap_usd=1,suite_cost_cap_usd=2)
    config['tokens']['token'].update(models=['c01','grader-01'],candidate_models=['c01'],
        budget_scopes={'suite:'+scope:2},item_scope_prefix='item:'+scope+':',
        allowed_item_ids=['one','two'],trace_scopes=[scope])
    return scope


def manifest():
    return {'protocol_version':1,'items':[{'id':'one'}],'selection':'fixed','aggregation':{'kind':'weighted_mean'}}


def normalize(item,config,started):
    return normalize_result({'protocol_version':1,'items':[item]},manifest(),
        Ledger(Path(config['artifacts'])/'ledger.sqlite'),'test',started,Path(config['artifacts']).parent/'research-jobs',candidate_model='c01')


@pytest.mark.parametrize('usage',[True,False])
def test_sdk_grader_evidence_is_charged_to_same_item_with_unknown_usage_retained(anthropic_provider,tmp_path,usage):
    server,config=anthropic_provider;scope=suite_access(config)
    server.responses=[answer('candidate answer'),answer('yes',usage=usage)]
    started=time.time()
    with TestClient(research_app(config)) as client:
        sdk=connect_sdk(tmp_path,client,config,'token')
        def grade(text):
            reply=sdk.chat('Judge: '+text,model='grader-01',item_id='one')
            return {'score':float(reply['text']=='yes'),'answer_status':'answered','evidence':[reply['evidence']]}
        item=sdk.item('one','question',grade)
    result=normalize(item,config,started)
    ledger=Ledger(Path(config['artifacts'])/'ledger.sqlite')
    check_budget_evidence(result,manifest(),ledger,scope,tmp_path)
    assert result['score']==1 and len(item['evidence'])==2
    assert [b['model'] for b in server.bodies]==['secret-model','private-grader']
    with ledger.connect() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM calls ORDER BY created')]
        scopes=[{r[0] for r in db.execute('SELECT scope FROM call_budget_scopes WHERE call_id=?',(row['id'],))} for row in rows]
    assert scopes[0]==scopes[1]=={'suite:'+scope,'item:'+scope+':'+hashlib.sha256(b'one').hexdigest()}
    assert {r['wallet'] for r in rows}=={'test'} and len(rows)==2
    wallet=ledger.status('test')[0]
    assert wallet['charged']==pytest.approx(.00007 if usage else .00002)
    assert (wallet['outstanding']>0)==(not usage)
    assert rows[1]['charged']==(.00005 if usage else None)


@pytest.mark.parametrize('kind',['infra','budget','wrong_item','wrong_wallet'])
def test_grader_failure_or_misattribution_never_becomes_candidate_wrong_answer(anthropic_provider,tmp_path,kind):
    server,config=anthropic_provider;scope=suite_access(config)
    server.responses=[answer('candidate answer')]
    started=time.time()
    if kind=='infra':server.responses += [(503,b'{"error":{"type":"overloaded_error"}}',{})]*4
    elif kind!='budget':server.responses += [answer('yes')]
    if kind=='budget':config['prices']['grader-01']['output']=100
    if kind=='wrong_wallet':
        config['tokens']['other']=dict(config['tokens']['token'],wallet='other',
            budget_scopes={},item_scope_prefix='item:other:',trace_scopes=['d'*32])
    with TestClient(research_app(config)) as client:
        sdk=connect_sdk(tmp_path,client,config,'token')
        helper=sdk
        if kind=='wrong_wallet':helper=connect_sdk(tmp_path,client,config,'other')
        def grade(text):
            reply=helper.chat('Judge: '+text,model='grader-01',item_id='two' if kind=='wrong_item' else 'one')
            return {'score':1,'answer_status':'answered','evidence':[reply['evidence']]}
        item=sdk.item('one','question',grade)
    if kind in ('infra','budget'):
        assert item['score'] is None and item['error_stage']=='grading'
        assert item['execution_status']==('infra_error' if kind=='infra' else 'budget_exhausted')
        assert normalize(item,config,started)['score_status']=='incomplete'
        assert sum(b['model']=='secret-model' for b in server.bodies)==1
        assert len(server.bodies)==(5 if kind=='infra' else 1)
        wallet=Ledger(Path(config['artifacts'])/'ledger.sqlite').status('test')[0]
        assert wallet['charged']==pytest.approx(.00002)
        assert (wallet['outstanding']>0)==(kind=='infra')
    elif kind=='wrong_wallet':
        with pytest.raises(ValueError,match='execution/wallet'):normalize(item,config,started)
    else:
        result=normalize(item,config,started)
        with pytest.raises(ValueError,match='another item'):
            check_budget_evidence(result,manifest(),Ledger(Path(config['artifacts'])/'ledger.sqlite'),scope,tmp_path)


def test_native_helper_uses_frozen_effort_and_separate_model_cost(native_provider,tmp_path):
    server,received,_=native_provider
    path,cfg=helper_config(tmp_path)
    cfg['providers']['native-helper']={'upstream':f'http://127.0.0.1:{server.server_port}/v1',
        'key_env':'UNUSED_FIXTURE_KEY','wire_api':'openai_responses'}
    cfg['auxiliary_models'][0].update(provider='native-helper',model='private-native-grader',effort='medium',
        native_limits={'max_context_tokens':1050000,'max_output_tokens':128000})
    path.write_text(yaml.safe_dump(cfg));loaded=load(path);out=tmp_path/'run';out.mkdir()
    config,tokens=build_gateway(loaded,loaded['researchers'][0],out,tmp_path/'sock',
        mock_url=f'http://127.0.0.1:{server.server_port}/v1')
    # This is the scoped context normally created by run_suite, not researcher direct access.
    config['tokens'][tokens['development']].update(research=False,models=['dev-a','grader-01'],
        candidate_models=['dev-a'],budget_scopes={'suite:native-helper':2},
        item_scope_prefix='item:native-helper:',allowed_item_ids=['one'])
    with TestClient(research_app(config)) as client:
        sdk=connect_sdk(tmp_path,client,config,tokens['development'])
        sdk.context['efforts']=config['efforts']
        reply=sdk.chat('Judge fixture',model='grader-01',item_id='one')
        assert reply['text']=='READY' and reply['response']['model']=='grader-01'
    assert len(received)==1 and received[0]['path']=='/v1/responses'
    assert received[0]['body']['reasoning']=={'effort':'medium'}
    report=accounting(out,loaded)
    row=next(r for r in report['by_model'] if r['model']=='grader-01')
    assert row['wallet']=='development' and row['calls']==1
    assert row['cache_adjusted_estimate_usd']==pytest.approx(.000116)


def test_full_isolated_pipeline_charges_candidate_simulator_and_grader_once(tmp_path,monkeypatch):
    root=os.environ.get('SEB_TEST_ROOT');science=os.environ.get('SEB_TEST_SCIENCE')
    if not root or not science:pytest.skip('Set sandbox paths for auxiliary integration')
    import seb.experiment as experiment
    path,cfg=helper_config(tmp_path)
    cfg['runtime']={'rootfs':root,'science_packages':science}
    path.write_text(yaml.safe_dump(cfg))
    original=experiment.mock_submission
    def submission(work):
        original(work)
        (work/'submission/run.py').write_text('''import argparse,json
from pathlib import Path
from research_sdk import Client
p=argparse.ArgumentParser();p.add_argument('--context');p.add_argument('--output');a=p.parse_args()
c=Client(a.context);items=[]
assert c.info()['models']==[c.context['model']]
assert set(c.context['efforts'])=={c.context['model'],'grader-01','simulator-01'}
assert {m['id'] for m in c.context['auxiliary_models']}=={'grader-01','simulator-01'}
for ident,prompt,answer in [('sum','What is 2 + 2?','4'),('product','What is 2 * 3?','6')]:
    simulation=c.chat('Simulated user fixture',model='simulator-01',item_id=ident)
    def grade(text):
        reply=c.chat('Grader fixture: '+text,model='grader-01',item_id=ident)
        return {'score':float(text.strip()==answer and reply['text']=='4'),
                'answer_status':'answered','evidence':[simulation['evidence'],reply['evidence']]}
    items.append(c.item(ident,prompt,grade))
    path=Path(a.output);temp=path.with_suffix('.tmp');temp.write_text(json.dumps({'protocol_version':1,'items':items}));temp.replace(path)
''')
    monkeypatch.setattr(experiment,'mock_submission',submission)
    out=tmp_path/'run';result=experiment.run(path,None,out,mock=True)
    assert result['eligible'] and result['complete_models']==result['expected_models']==6
    assert result['accounting']['unknown_calls']==0
    ledger=Ledger(out/'gateway/ledger.sqlite')
    with ledger.connect() as db:
        counts={r['model']:r['n'] for r in db.execute('SELECT model,COUNT(*) AS n FROM calls GROUP BY model')}
        # Legacy example measures three development and all six acceptance candidates.
        assert counts['grader-01']==counts['simulator-01']==18
        assert sum(counts.values())==54
        assert {r[0] for r in db.execute('SELECT DISTINCT wallet FROM calls')}=={'development','evaluation'}
    jobs=list((out/'research-jobs').glob('*/execution/result.json'))
    assert len(jobs)==9
    for path in jobs:
        state=json.loads(path.read_text())
        assert state['status']=='ok' and state['cost']['calls']==6
        for item in state['result']['items']:assert len(item['evidence'])==3
    assert not list(out.glob('*.secret'))


def test_helper_only_evidence_cannot_stand_in_for_evaluated_candidate(anthropic_provider,tmp_path):
    server,config=anthropic_provider;suite_access(config)
    server.responses=[answer('yes')];started=time.time()
    with TestClient(research_app(config)) as client:
        sdk=connect_sdk(tmp_path,client,config,'token')
        item=sdk.item('one','Solve instead of candidate',lambda text:1,model='grader-01')
    with pytest.raises(ValueError,match='evaluated candidate'):normalize(item,config,started)
    assert len(server.bodies)==1
