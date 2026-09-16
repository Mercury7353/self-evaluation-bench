import json
import math
import os
from pathlib import Path
import shutil

import pytest
import yaml

from seb.experiment_config import load, researcher_view
from seb.overall import macro_spearman
from seb.scoring import with_constants
from seb.supervisor import check_designer_exit


EXAMPLES=Path(__file__).parents[1]/'examples'


def fixture_config(tmp_path):
    cfg=yaml.safe_load((EXAMPLES/'mock.yaml').read_text())
    cfg['runtime']={'rootfs':str(tmp_path/'rootfs'),'science_packages':str(tmp_path/'science')}
    for visibility in ['whitebox','blackbox']:
        cfg['benchmarks'][visibility][0]['reference']=str(EXAMPLES/'references'/('visible.example.json' if visibility=='whitebox' else 'hidden.example.json'))
    python=tmp_path/'rootfs/usr/local/bin/python';python.parent.mkdir(parents=True);python.touch()
    (tmp_path/'science').mkdir()
    path=tmp_path/'config.yaml';path.write_text(yaml.safe_dump(cfg))
    return path,cfg


def test_researcher_view_withholds_targets_and_holdouts(tmp_path):
    path,_=fixture_config(tmp_path)
    view=researcher_view(load(path))
    assert len(view['models'])==3
    encoded=json.dumps(view)
    assert 'hidden-target' not in encoded and 'holdout' not in encoded
    assert 'REPLACE' not in encoded and 'upstream' not in encoded
    assert set(view['targets']['visible-target']['scores'])==set(view['models'])


@pytest.mark.parametrize('mutation', ['overlap_family','overlap_id','bad_budget','unknown_field','blackbox_resource','duplicate_target','nan_reference'])
def test_invalid_experiment_fails_before_spending(tmp_path,mutation):
    path,cfg=fixture_config(tmp_path)
    if mutation=='overlap_family':cfg['models'][3]['family']=cfg['models'][0]['family']
    elif mutation=='overlap_id':cfg['researchers'][0]['id']=cfg['models'][0]['id']
    elif mutation=='bad_budget':cfg['budgets']['development_usd']=-1
    elif mutation=='unknown_field':cfg['budget']=5
    elif mutation=='blackbox_resource':cfg['benchmarks']['blackbox'][0]['resources']=[str(tmp_path)]
    elif mutation=='duplicate_target':cfg['benchmarks']['blackbox'][0]['id']=cfg['benchmarks']['whitebox'][0]['id']
    else:
        bad=tmp_path/'bad.json';bad.write_text('{"dev-a":NaN}')
        cfg['benchmarks']['blackbox'][0]['reference']=str(bad)
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError):load(path)


def test_overall_never_silently_drops_missing_target_or_models():
    expected={'one':4,'two':4,'tiny':2}
    result=macro_spearman({'one':{'n':4,'spearman':1},'two':{'n':3,'spearman':.9}},expected)
    assert result['score'] is None and result['diagnostic_available_target_mean']==1
    assert result['components']['two']['status']=='incomplete_coverage'
    assert result['excluded_targets']=={'tiny':'insufficient_frozen_reference_coverage'}
    result=macro_spearman({'one':{'n':4,'spearman':1},'two':{'n':4,'spearman':-1}},expected)
    assert result['score']==0 and result['eligible_targets']==2


def test_constant_predictions_zero_but_constant_reference_undefined():
    constant={'targets':{'t':{'n_predicted':3,'spearman':None}},'rows':[
        {'targets':{'t':v},'predictions':{'t':{'score':.5}}} for v in [0,.5,1]]}
    row=with_constants(constant)['targets']
    assert macro_spearman(row,{'t':3})['score']==0
    assert macro_spearman(row,{'t':3},constant_prediction='undefined')['score'] is None
    for r in constant['rows']:r['targets']['t']=1
    row=with_constants(constant)['targets']
    assert macro_spearman(row,{'t':3})['score'] is None


def test_researcher_zero_exit_error_is_failure(tmp_path):
    (tmp_path/'claude.stdout').write_text(json.dumps({'type':'result','is_error':True,'result':'terminal error'})+'\n')
    with pytest.raises(RuntimeError):check_designer_exit(tmp_path,0)


def test_codex_configuration_builds_only_bounded_native_researcher_access(tmp_path,monkeypatch):
    from seb.experiment import build_gateway
    path,cfg=fixture_config(tmp_path)
    researcher=cfg['researchers'][0]
    researcher.update(harness='codex',effort='xhigh',model='gpt-5.5-2026-04-23',
        native_limits={'max_output_tokens':128000,'max_context_tokens':1050000})
    path.write_text(yaml.safe_dump(cfg))
    loaded=load(path)
    out=tmp_path/'run';out.mkdir()
    gateway,tokens=build_gateway(loaded,loaded['researchers'][0],out,tmp_path/'gateway.sock',mock_url='http://127.0.0.1:9/v1')
    entry=gateway['tokens'][tokens['designer']]
    assert entry['native_responses'] and entry['wallet']=='designer'
    assert entry['cap']==cfg['budgets']['researcher_usd'] and entry['deadline_epoch']>0
    assert gateway['native_researcher']['model']==researcher['model']
    assert not gateway['tokens'][tokens['development']].get('native_responses')
    del researcher['native_limits'];path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError,match='native_limits'):load(path)


def test_codex_completion_requires_terminal_turn_but_accepts_nonfatal_item(tmp_path):
    path=tmp_path/'codex.stdout'
    events=[{'type':'item.completed','item':{'type':'error','message':'nonfatal'}},{'type':'turn.completed'}]
    path.write_text('\n'.join(map(json.dumps,events)))
    check_designer_exit(tmp_path,0,harness='codex')
    path.write_text(json.dumps({'type':'turn.failed','error':{'message':'failure'}}))
    with pytest.raises(RuntimeError):check_designer_exit(tmp_path,0,harness='codex')
    path.write_text(json.dumps({'type':'turn.started'}))
    with pytest.raises(RuntimeError):check_designer_exit(tmp_path,0,harness='codex')


def test_mock_full_pipeline(tmp_path,monkeypatch):
    root=os.environ.get('SEB_TEST_ROOT');science=os.environ.get('SEB_TEST_SCIENCE')
    if not root or not science:pytest.skip('Set SEB_TEST_ROOT and SEB_TEST_SCIENCE for offline namespace integration')
    from seb.experiment import run
    path,cfg=fixture_config(tmp_path)
    cfg['runtime']={'rootfs':root,'science_packages':science}
    cfg['design']['rounds']=2
    cfg['evaluation']['preflight']=True
    cfg['budgets']['judge_usd']=2
    path.write_text(yaml.safe_dump(cfg));out=tmp_path/'run'
    result=run(path,None,out,mock=True)
    assert json.loads((out/'preflight.json').read_text())['transport_complete']
    assert result['eligible'] and result['paid_api_calls']==0
    assert result['complete_models']==result['expected_models']==6
    assert result['overall']['score'] is not None
    dev=json.loads((out/'development-1/results.json').read_text())
    reused=json.loads((out/'development-2/results.json').read_text())
    assert {r['job_id'] for r in dev}=={r['job_id'] for r in reused}
    assert result['accounting']['unknown_calls']==0
    freeze=json.loads((out/'freeze.json').read_text())
    assert freeze['selected_round']==2
    assert 'hidden-target' not in (out/'researcher-work/whitebox.json').read_text()
    assert not list(out.glob('*.secret')) and not (out/'gateway.private.json').exists()
    assert not (out/'researcher-work/access.json').exists()
    with pytest.raises(FileExistsError):run(path,None,out,mock=True)


def test_wallet_closed_by_overshoot_is_not_budget_success(tmp_path):
    from seb.experiment import accounting
    from seb.ledger import Ledger
    ledger=Ledger(tmp_path/'gateway/ledger.sqlite');ledger.wallet('evaluation',20)
    ledger.reserve('a','evaluation','candidate',.1)
    ledger.finish('a',.2,{'input_tokens':1,'output_tokens':1},'completed')
    with ledger.connect() as db:db.execute("UPDATE wallets SET cap=0 WHERE name='evaluation'")
    cfg={'budgets':{'researcher_usd':50,'development_usd':30,'evaluation_usd':20},
         'models':[{'id':'candidate','price':{'input':1,'output':1}}],'researchers':[]}
    result=accounting(tmp_path,cfg)
    assert result['within_budget'] is False and result['closed_wallets']==['evaluation']


def test_domain_protocol_accepts_known_families_but_rejects_multiple_runs(tmp_path):
    path,cfg=fixture_config(tmp_path)
    cfg['domain_protocol']={'version':1,'minimum_models':3,'minimum_families':2}
    cfg['design']['rounds']=1
    cfg['models'][3]['family']=cfg['models'][0]['family']
    for visibility in ['whitebox','blackbox']:
        cfg['benchmarks'][visibility].append(dict(cfg['benchmarks'][visibility][0],id=visibility+'-second'))
    path.write_text(yaml.safe_dump(cfg))
    loaded=load(path)
    assert loaded['models'][3]['family']==loaded['models'][0]['family']
    cfg['design']['rounds']=2;path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError,match='one independent'):load(path)


def test_domain_offline_pipeline_only_accepts_holdout_and_never_fits_sealed(tmp_path,monkeypatch):
    root=os.environ.get('SEB_TEST_ROOT');science=os.environ.get('SEB_TEST_SCIENCE')
    if not root or not science:pytest.skip('Set sandbox paths for integration')
    import seb.experiment as experiment
    path,cfg=fixture_config(tmp_path)
    cfg['runtime']={'rootfs':root,'science_packages':science}
    cfg['domain_protocol']={'version':1,'minimum_models':3,'minimum_families':2}
    cfg['design']['rounds']=1
    cfg['models'][3]['family']=cfg['models'][0]['family']
    for visibility in ['whitebox','blackbox']:
        cfg['benchmarks'][visibility].append(dict(cfg['benchmarks'][visibility][0],id=visibility+'-second'))
    original=experiment.mock_submission
    def submission(work):
        original(work)
        (work/'submission/predictor.py').write_text('''
def fit(training_rows,target_metadata):
    assert set(target_metadata)=={'visible-target','whitebox-second'}
    assert all(set(row['targets'])==set(target_metadata) for row in training_rows)
    return list(target_metadata)
def predict(fitted,observations):
    return {t:sum(observations.values())/len(observations) for t in fitted}
''')
    monkeypatch.setattr(experiment,'mock_submission',submission)
    path.write_text(yaml.safe_dump(cfg));out=tmp_path/'domain-run'
    result=experiment.run(path,None,out,mock=True)
    assert result['eligible'] and result['expected_models']==3
    assert result['overall']['source']=='domain_protocol_v1'
    assert 'sealed_utility' in result and 'visible_utility' in result
    accepted=json.loads((out/'acceptance-jobs/results.json').read_text())
    assert {r['model'] for r in accepted}=={m['id'] for m in cfg['models'] if m['split']=='holdout'}
    payload=(out/'domain/visible-prediction/input.json').read_text()
    assert 'hidden-target' not in payload and 'blackbox-second' not in payload
    assert not (out/'blackbox/family-cv').exists()
    assert not (out/'domain/sealed-predictor').exists()


def test_domain_missing_reference_blocks_before_provider_access(tmp_path):
    path,cfg=fixture_config(tmp_path)
    cfg['domain_protocol']={'version':1,'minimum_models':3,'minimum_families':2}
    cfg['design']['rounds']=1
    for visibility in ['whitebox','blackbox']:
        cfg['benchmarks'][visibility].append(dict(cfg['benchmarks'][visibility][0],id=visibility+'-second'))
    incomplete=tmp_path/'incomplete.json';incomplete.write_text('{"dev-a":1,"dev-b":0,"dev-c":0.5,"holdout-a":1}')
    cfg['benchmarks']['blackbox'][0]['reference']=str(incomplete)
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError,match='Insufficient frozen holdout reference'):load(path)


def joint_fixture(tmp_path):
    path,cfg=fixture_config(tmp_path)
    domains=['coding','co-work','reasoning']
    cfg['domain_protocol']={'version':2,'domains':domains,'minimum_models':3,'minimum_families':2}
    cfg['design']['rounds']=1
    cfg['budgets'].update(development_usd=100,suite_usd=30,evaluation_usd=90)
    for visibility in ('whitebox','blackbox'):
        template=cfg['benchmarks'][visibility][0]
        cfg['benchmarks'][visibility]=[dict(template,id=d+'-'+visibility+'-'+str(i),domain=d)
                                      for d in domains for i in range(2)]
    path.write_text(yaml.safe_dump(cfg))
    return path,cfg


@pytest.mark.parametrize('mutation',['missing_target','unknown_domain','duplicate_domain','more_runs'])
def test_joint_invalid_target_partition_cannot_start(tmp_path,mutation):
    path,cfg=joint_fixture(tmp_path)
    if mutation=='missing_target':cfg['benchmarks']['blackbox'].pop()
    elif mutation=='unknown_domain':cfg['benchmarks']['whitebox'][0]['domain']='outside'
    elif mutation=='duplicate_domain':cfg['domain_protocol']['domains'][1]='coding'
    else:cfg['design']['rounds']=3
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError):load(path)


def test_joint_view_and_gateway_share_budget_without_exposing_sealed_metadata(tmp_path):
    from seb.experiment import build_gateway,prepare_workspace
    path,cfg=joint_fixture(tmp_path);loaded=load(path)
    view=researcher_view(loaded)
    assert len(view['targets'])==6 and set(view['domains'])=={'coding','co-work','reasoning'}
    assert all(len(v)==2 for v in view['domains'].values())
    out=tmp_path/'run';out.mkdir()
    config,tokens=build_gateway(loaded,loaded['researchers'][0],out,tmp_path/'gateway.sock',mock_url='http://127.0.0.1:9')
    work=prepare_workspace(loaded,config,tokens,out)
    public='\n'.join((work/name).read_text() for name in ('whitebox.json','CONTRACT.md','access.json'))
    for t in cfg['benchmarks']['blackbox']:assert t['id'] not in public
    for m in cfg['models'][3:]:assert m['id'] not in public
    assert config['tokens'][tokens['development']]['cap']==100
    assert config['suite_cost_cap_usd']==30
    assert config['tokens'][tokens['evaluation']]['cap']==90
    assert 'joint_domains' in json.loads((work/'access.json').read_text())


def test_joint_offline_pipeline_measures_once_for_all_twelve_targets(tmp_path,monkeypatch):
    root=os.environ.get('SEB_TEST_ROOT');science=os.environ.get('SEB_TEST_SCIENCE')
    if not root or not science:pytest.skip('Set sandbox paths for joint integration')
    import seb.experiment as experiment
    from seb.ledger import Ledger
    path,cfg=joint_fixture(tmp_path)
    cfg['runtime']={'rootfs':root,'science_packages':science}
    original=experiment.mock_submission;research_runs=[]
    def submission(work):
        research_runs.append(work);original(work)
        path=work/'submission/evaluation.json';manifest=json.loads(path.read_text())
        manifest['domain_aggregations']={
            'coding':{'kind':'weighted_mean','weights':{'sum':1}},
            'co-work':{'kind':'weighted_mean','weights':{'product':1}},
            'reasoning':{'kind':'weighted_mean','weights':{'sum':1,'product':1}}}
        path.write_text(json.dumps(manifest))
        (work/'submission/predictor.py').write_text('''
def fit(training_rows,target_metadata):
    assert len(target_metadata)==6
    assert all('whitebox' in t for t in target_metadata)
    assert all(set(row['targets'])==set(target_metadata) for row in training_rows)
    assert all(set(row['observations'])=={'sum','product'} for row in training_rows)
    return list(target_metadata)
def predict(fitted,observations):
    return {t:sum(observations.values())/len(observations) for t in fitted}
''')
    monkeypatch.setattr(experiment,'mock_submission',submission)
    path.write_text(yaml.safe_dump(cfg));out=tmp_path/'joint-run'
    result=experiment.run(path,None,out,mock=True)
    assert result['eligible'] and result['research_unit']=='joint' and result['expected_models']==3
    assert result['overall']['source']=='joint_protocol_v2'
    assert len(research_runs)==1 and set(result['domains'])=={'coding','co-work','reasoning'}
    report=json.loads((out/'domain/scores.json').read_text())
    assert len(report['visible'])==len(report['sealed'])==6
    with Ledger(out/'gateway/ledger.sqlite').connect() as db:
        calls={r['wallet']:r['n'] for r in db.execute('SELECT wallet,COUNT(*) n FROM calls GROUP BY wallet')}
    assert calls=={'development':6,'evaluation':6}  # Two items, three models each; no domain multiplier.
    accepted=json.loads((out/'acceptance-jobs/results.json').read_text())
    assert len(accepted)==3 and len({r['job_id'] for r in accepted})==3
    for row in accepted:
        item_scores={i['id']:i['score'] for i in row['result']['items']}
        z=row['result']['domain_scores']
        assert z['coding']==item_scores['sum'] and z['co-work']==item_scores['product']
        assert z['reasoning']==sum(item_scores.values())/2
    payload=(out/'domain/visible-prediction/input.json').read_text()
    assert 'blackbox' not in payload and 'holdout' not in payload and 'family' not in payload
    assert not (out/'domain/sealed-predictor').exists()


def test_completed_development_job_is_read_after_deadline_without_resubmission(tmp_path,monkeypatch):
    import seb.supervisor as supervisor
    (tmp_path/'candidate.job.json').write_text(json.dumps({'id':'existing-job'}))
    calls=[]
    def request(socket,token,path,*args):
        calls.append(path)
        assert path=='/research/jobs/existing-job'
        return {'status':'ok','score_status':'valid'}
    monkeypatch.setattr(supervisor,'request',request)
    rows=supervisor.run_jobs({'gateway_socket':'unused'},'scoped',['candidate'],'submission',tmp_path,1)
    assert rows[0]['score_status']=='valid' and calls==['/research/jobs/existing-job']
    rows=supervisor.run_jobs({'gateway_socket':'unused'},'scoped',['not-submitted'],'submission',tmp_path,1)
    assert rows[0]['score_status']=='incomplete' and len(calls)==1


def test_separate_acceptance_concurrency(tmp_path):
    from seb.experiment import build_gateway
    path,_=fixture_config(tmp_path)
    raw=yaml.safe_load(path.read_text());raw['evaluation']['model_concurrency']=2
    raw['evaluation']['acceptance_model_concurrency']=4
    path.write_text(yaml.safe_dump(raw));cfg=load(path)
    out=tmp_path/'separate';out.mkdir()
    config,_=build_gateway(cfg,cfg['researchers'][0],out,tmp_path/'gateway.sock',mock_url='http://localhost:9')
    assert config['suite_concurrency']==2
    assert config['acceptance_suite_concurrency']==4


def test_raw_full_panel_resumes_partial_dev_without_repeating_answers(tmp_path,monkeypatch):
    root=os.environ.get('SEB_TEST_ROOT');science=os.environ.get('SEB_TEST_SCIENCE')
    if not root or not science:pytest.skip('Set sandbox paths for raw full-panel integration')
    import seb.experiment as experiment
    from seb.supervisor import run_jobs
    from seb.ledger import Ledger
    import time
    path,cfg=joint_fixture(tmp_path)
    cfg['runtime']={'rootfs':root,'science_packages':science}
    cfg['domain_protocol']['score_mode']='raw_domain';cfg['overall']={'metric':'macro_pearson','source':'raw_domain'}
    cfg['design']['final_development_measurement']=False
    cfg['design']['seconds']=cfg['design']['checkpoint_seconds']=120
    original=experiment.mock_submission
    def submission(work):
        original(work);source=work/'submission';p=source/'evaluation.json';manifest=json.loads(p.read_text())
        manifest['questions']=[{'id':'sum','prompt':'What is 2 + 2?'},{'id':'product','prompt':'What is 2 * 3?'}]
        for row in manifest['items']:row['question_id']=row['id']
        manifest['domain_aggregations']={d:{'kind':'weighted_mean','weights':{'sum':1,'product':1}} for d in ['coding','co-work','reasoning']}
        p.write_text(json.dumps(manifest))
        program=(source/'run.py').read_text()
        # Deliberate process failure after persisting the first measured item.
        program += "\n"
        program=program.replace("temp.replace(path)","temp.replace(path)\n    if c.context['model']=='dev-a' and ident=='sum' and not c.completed_items:raise RuntimeError('fixture interruption after answer')")
        (source/'run.py').write_text(program)
        config=json.loads((work.parent/'gateway.private.json').read_text());access=json.loads((work/'access.json').read_text())
        rows=run_jobs(config,access['token'],['dev-a','dev-b'],'submission',work.parent/'fixture-development',time.time()+90)
        assert {x['model']:x['status'] for x in rows}=={'dev-a':'error','dev-b':'ok'}
    monkeypatch.setattr(experiment,'mock_submission',submission)
    path.write_text(yaml.safe_dump(cfg));out=tmp_path/'raw-run'
    result=experiment.run(path,None,out,mock=True)
    assert result['complete_models']==result['expected_models']==6
    assert result['predictor']=='not_used_raw_domain' and result['measurement_complete']
    assert not list((out/'domain').glob('*predict*'))
    accepted=json.loads((out/'acceptance-jobs/results.json').read_text())
    assert {r['model'] for r in accepted}=={m['id'] for m in cfg['models']}
    with Ledger(out/'gateway/ledger.sqlite').connect() as db:
        calls={r['model']:r['n'] for r in db.execute('SELECT model,COUNT(*) n FROM calls GROUP BY model')}
        assert calls=={m['id']:2 for m in cfg['models']}
        wallets={r['wallet']:r['n'] for r in db.execute('SELECT wallet,COUNT(*) n FROM calls GROUP BY wallet')}
        assert wallets=={'development':3,'evaluation':9}
    for row in accepted:assert row['score_status']=='valid' and len(row['result']['items'])==2
    report=json.loads((out/'domain/scores.json').read_text());assert report['metric']=='pearson'
    assert 'Pearson' in (out/'researcher-work/CONTRACT.md').read_text()
