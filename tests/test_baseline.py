import json
import os
from pathlib import Path
import sqlite3

import pytest
import yaml

from seb.baseline import reuse_development_ledger
from seb.campaign import Registry, run_baseline_episode
from seb.experiment import accounting, build_gateway, mock_submission
from seb.experiment_config import load
from seb.ledger import BudgetExceeded, Ledger
from test_experiment import joint_fixture


def calls(path):
    with sqlite3.connect(path) as db:
        return db.execute('SELECT * FROM calls ORDER BY id').fetchall()


def test_existing_wallet_is_shared_not_copied_and_unknown_stays_reserved(tmp_path):
    source=tmp_path/'old/ledger.sqlite';old=Ledger(source);old.wallet('development',1)
    old.reserve('a','development','old-model',.4);old.finish('a',.3,{'output_tokens':3},'completed')
    old.reserve('b','development','old-model',.6,scopes={'old-scope':.6})
    old.finish('b',None,{},'transport_unknown')
    before=calls(source);out=tmp_path/'run';out.mkdir()
    inherited=reuse_development_ledger(out,source,1)
    alias=out/'gateway/ledger.sqlite';current=Ledger(alias)
    assert alias.is_symlink() and current.path==old.path
    assert calls(source)==before
    assert inherited['charged_usd']==.3 and inherited['outstanding_reserved_usd']==.6
    with pytest.raises(BudgetExceeded):current.reserve('c','development','new-model',.2)
    with old.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM call_budget_scopes').fetchone()[0]==1
    another=tmp_path/'another';another.mkdir()
    with pytest.raises(ValueError,match='already assigned'):reuse_development_ledger(another,source,1)
    assert not (another/'gateway/ledger.sqlite').exists()
    assert calls(source)==before


@pytest.mark.parametrize('kind',['missing','raised_cap','closed','extra_wallet'])
def test_reuse_cannot_create_reset_or_mix_existing_wallets(tmp_path,kind):
    source=tmp_path/'old/ledger.sqlite'
    if kind!='missing':
        old=Ledger(source);old.wallet('development',0 if kind=='closed' else 1)
        if kind=='extra_wallet':old.wallet('evaluation',1)
    out=tmp_path/'run';out.mkdir()
    with pytest.raises((ValueError,FileNotFoundError)):
        reuse_development_ledger(out,source,2 if kind=='raised_cap' else 1)
    assert not (out/'gateway/ledger.sqlite').exists()
    if kind=='missing':assert not source.exists()


def test_baseline_gateway_has_no_researcher_wallet_or_provider_access(tmp_path):
    path,cfg=joint_fixture(tmp_path)
    cfg['providers']['unavailable-researcher']={'upstream':'https://not-used.invalid','key_env':'UNSET_RESEARCHER_ONLY'}
    cfg['researchers'][0]['provider']='unavailable-researcher';path.write_text(yaml.safe_dump(cfg))
    loaded=load(path);out=tmp_path/'run';out.mkdir()
    gateway,tokens=build_gateway(loaded,None,out,tmp_path/'socket',mock_url='http://127.0.0.1:9')
    assert set(tokens)=={'development','evaluation'}
    assert {v['wallet'] for v in gateway['tokens'].values()}=={'development','evaluation'}
    assert set(gateway['model_backends'])=={m['id'] for m in cfg['models']}
    assert 'native_researcher' not in gateway and not (out/'unavailable-researcher.secret').exists()


def test_accounting_keeps_inherited_charges_without_repricing_old_usage(tmp_path):
    source=tmp_path/'old/ledger.sqlite';old=Ledger(source);old.wallet('development',1)
    old.reserve('a','development','same-model',.1)
    old.finish('a',.1,{'input_tokens':500,'output_tokens':500},'completed')
    old.reserve('b','development','legacy-model',.2);old.finish('b',None,{},'transport_unknown')
    out=tmp_path/'run';out.mkdir();reuse_development_ledger(out,source,1)
    current=Ledger(out/'gateway/ledger.sqlite')
    current.reserve('c','development','same-model',.01)
    current.finish('c',.0002,{'input_tokens':100,'output_tokens':100},'completed')
    cfg={'models':[{'id':'same-model','price':{'input':1,'output':1}}],'researchers':[],
         'budgets':{'researcher_usd':0,'development_usd':1,'evaluation_usd':1}}
    bill=accounting(out,cfg)
    row=next(r for r in bill['by_model'] if r['model']=='same-model')
    assert row['metered_usd']==pytest.approx(.1002)
    assert row['cache_adjusted_estimate_usd'] is None
    assert row['cache_adjusted_known_component_usd']==pytest.approx(.0002)
    assert row['inherited_calls']==1 and bill['unknown_calls']==1 and bill['status']=='pending'
    assert bill['wallets'][0]['outstanding']==.2


def registered_fixture(tmp_path):
    path,cfg=joint_fixture(tmp_path)
    source=tmp_path/'old/ledger.sqlite';old=Ledger(source);old.wallet('development',100)
    manifest={'research_unit':'joint','researchers':cfg['researchers'],'candidates':cfg['models'],
        'domains':[{'id':d,'targets':[dict(t,visibility=v) for v,key in [('visible','whitebox'),('sealed','blackbox')]
            for t in cfg['benchmarks'][key] if t['domain']==d]} for d in cfg['domain_protocol']['domains']],
        'budgets':{'development_usd':100,'researcher_usd':200,'candidate_suite_usd':30,'research_seconds':cfg['design']['seconds']},
        'budget_ablation':{'researchers':[],'additional_development_usd':[]},'baselines':['fixed'],
        'run_allocation_ceiling_usd':1000,
        'preflight_allocation':{'episode_id':'baseline--fixed--joint--b100','wallet':'development',
            'counts_within_development_usd':100,'reuse_existing_ledger':str(source)}}
    root=tmp_path/'campaign';reg=Registry(root,manifest)
    return path,cfg,source,root,reg


def test_campaign_baseline_uses_registered_wallet_and_is_claimed_only_once(tmp_path,monkeypatch):
    path,cfg,source,root,reg=registered_fixture(tmp_path);invoked=[]
    def fake_run(*args,**kwargs):invoked.append((args,kwargs));return {'eligible':True}
    monkeypatch.setattr('seb.baseline.run',fake_run)
    episode='baseline--fixed--joint--b100'
    run_baseline_episode(root,episode,path,tmp_path/'submission','systemd:existing.service',mock=True)
    assert invoked[0][1]['reuse_ledger']==str(source)
    assert invoked[0][0][2]==str(root/'runs'/episode)
    assert invoked[0][1]['expected_config_sha256']==load(path)['_config_sha256']
    with pytest.raises(ValueError,match='Already claimed'):
        run_baseline_episode(root,episode,path,tmp_path/'submission','systemd:replacement.service',mock=True)
    assert len(invoked)==1


@pytest.mark.parametrize('mutation',['budget','model','target','missing_ledger'])
def test_campaign_mismatch_does_not_claim_or_launch(tmp_path,monkeypatch,mutation):
    path,cfg,source,root,reg=registered_fixture(tmp_path)
    if mutation=='budget':cfg['budgets']['evaluation_usd']+=1
    elif mutation=='model':cfg['models'][0]['model']='another-model'
    elif mutation=='target':cfg['benchmarks']['blackbox'][0]['id']='another-target'
    else:source.unlink()
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr('seb.baseline.run',lambda *a,**k:pytest.fail('Must not launch'))
    with pytest.raises(ValueError):run_baseline_episode(root,'baseline--fixed--joint--b100',path,tmp_path/'submission','test')
    assert all(r['status']=='planned' for r in reg.inventory())


def test_joint_baseline_pipeline_reuses_ledger_without_running_researcher(tmp_path,monkeypatch):
    rootfs=os.environ.get('SEB_TEST_ROOT');science=os.environ.get('SEB_TEST_SCIENCE')
    if not rootfs or not science:pytest.skip('Set sandbox paths for baseline integration')
    path,cfg,source,root,reg=registered_fixture(tmp_path)
    cfg['runtime']={'rootfs':rootfs,'science_packages':science};path.write_text(yaml.safe_dump(cfg))
    old=Ledger(source)
    old.reserve('a'*32,'development','old-probe',95);old.finish('a'*32,95,{'output_tokens':1},'completed')
    old.reserve('b'*32,'development','old-probe',.5);old.finish('b'*32,None,{},'transport_unknown')
    before=calls(source)
    workspace=tmp_path/'fixture';workspace.mkdir();mock_submission(workspace)
    submission=workspace/'submission';p=submission/'evaluation.json';data=json.loads(p.read_text())
    data['domain_aggregations']={d:{'kind':'weighted_mean','weights':{'sum':1,'product':1}} for d in cfg['domain_protocol']['domains']}
    p.write_text(json.dumps(data))
    (submission/'predictor.py').write_text('''
def fit(training_rows,target_metadata):
    assert len(target_metadata)==6 and all('whitebox' in t for t in target_metadata)
    assert all(set(row['targets'])==set(target_metadata) for row in training_rows)
    return list(target_metadata)
def predict(fitted,observations):
    return {t:sum(observations.values())/len(observations) for t in fitted}
''')
    monkeypatch.setattr('seb.experiment.launch_claude',lambda *a,**k:pytest.fail('No researcher allowed'))
    monkeypatch.setattr('seb.experiment.launch_codex',lambda *a,**k:pytest.fail('No researcher allowed'))
    result=run_baseline_episode(root,'baseline--fixed--joint--b100',path,submission,'test:baseline',mock=True)
    out=root/'runs/baseline--fixed--joint--b100'
    assert result['complete_models']==result['expected_models']==3 and result['research_unit']=='joint'
    assert result['researcher'] is None and result['researcher_calls']==result['paid_api_calls']==0
    assert not result['eligible'] and result['accounting']['unknown_calls']==1
    assert result['accounting']['within_budget'] and result['accounting']['status']=='pending'
    assert not list(out.glob('researcher-trace-*')) and not list(out.glob('*.secret'))
    assert not (out/'researcher-work/access.json').exists()
    assert (out/'gateway/ledger.sqlite').resolve()==source.resolve()
    assert [r for r in calls(source) if r[0] in {'a'*32,'b'*32}]==before
    with Ledger(source).connect() as db:
        counts={r['wallet']:r['n'] for r in db.execute('SELECT wallet,COUNT(*) n FROM calls GROUP BY wallet')}
        assert counts=={'development':8,'evaluation':6}
        assert db.execute("SELECT cap FROM wallets WHERE name='development'").fetchone()[0]==100
    report=json.loads((out/'domain/scores.json').read_text())
    assert len(report['visible'])==len(report['sealed'])==6
    assert 'blackbox' not in (out/'domain/visible-prediction/input.json').read_text()
