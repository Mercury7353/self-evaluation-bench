import json
import os
from pathlib import Path
import sys
import time

import pytest
from fastapi.testclient import TestClient
import yaml

from seb.container import run_logged
from seb.experiment import mock_submission
from seb.gateway import create_app
from seb.ledger import Ledger
from seb.research_lifecycle import ResearchLifecycle
from seb.supervisor import check_designer_exit
from test_experiment import joint_fixture


def lifecycle(tmp_path, *, predictor=True):
    work=tmp_path/'work';work.mkdir()
    out=tmp_path/'out';out.mkdir()
    config={'artifacts':str(out/'gateway'),'research_deadline_epoch':time.time()+120}
    return ResearchLifecycle(config,work,out,'r',minimum_items=2,require_predictor=predictor),work,out


def submission(work, version='first'):
    mock_submission(work)
    (work/'submission/predictor.py').write_text(f"VERSION={version!r}\ndef fit(rows, metadata): return list(metadata)\ndef predict(fitted, observations): return {{t: 0.5 for t in fitted}}\n")


def test_partial_edits_keep_latest_valid_snapshot_without_selecting_by_score(tmp_path):
    life,work,_=lifecycle(tmp_path)
    submission(work);life.capture(force=True)
    submission(work,'second');life.capture(force=True)
    (work/'submission/run.py').write_text('unfinished(')
    life.capture(force=True)
    selected=life.select()
    assert selected['sequence']==2 and len(life.entries)==2
    assert "VERSION='second'" in (Path(selected['path'])/'predictor.py').read_text()
    assert (work/'submission/run.py').read_text()=='unfinished('
    assert not (life.root/'staging').exists()
    assert life.last_invalid['error'].startswith('SyntaxError:')
    (Path(selected['path'])/'predictor.py').write_text('tampered')
    with pytest.raises(ValueError,match='changed after capture'):life.select()


def test_missing_predictor_and_post_deadline_edits_cannot_become_valid_snapshots(tmp_path):
    life,work,_=lifecycle(tmp_path)
    mock_submission(work);life.capture(force=True)
    with pytest.raises(RuntimeError,match='no valid saved'):life.select()
    submission(work);life.capture(force=True)
    selected=life.select()
    life.deadline=time.time()-1
    submission(work,'too-late');life.capture(force=True)
    assert life.select()==selected


def test_continuation_keeps_prior_snapshots_and_appends_without_replacing_them(tmp_path):
    life, work, out = lifecycle(tmp_path)
    submission(work); life.capture(force=True)
    prior = life.select().copy()
    restarted = ResearchLifecycle(life.config, work, out, 'r', minimum_items=2,
        require_predictor=True, resume=True, started=life.started)
    assert restarted.select() == prior and restarted.started == life.started
    submission(work, 'continued'); restarted.capture(force=True)
    assert restarted.select()['sequence'] == 2 and restarted.entries[0] == prior
    assert "VERSION='first'" in (Path(prior['path'])/'predictor.py').read_text()


@pytest.mark.parametrize('native',[False,True])
def test_actual_gateway_budget_denial_is_detected_without_upstream_or_budget_reset(tmp_path,native):
    life,work,out=lifecycle(tmp_path);submission(work)
    key=tmp_path/'key';key.write_text('test-only')
    entry={'wallet':'designer','cap':.000001,'models':['r'],'deadline_epoch':life.deadline}
    config={**life.config,'key_file':str(key),'upstream':'http://127.0.0.1:9',
            'prices':{'r':{'input':1,'output':1}},
            'tokens':{'test':entry},'model_backends':{'r':{'model':'test-model','key_file':str(key),'upstream':'http://127.0.0.1:9/v1'}}}
    if native:
        entry['native_responses']=True
        config['native_researcher']={'id':'r','model':'test-model','effort':'max','max_output_tokens':100,'max_context_tokens':1000}
    app=create_app(config)
    with TestClient(app) as client:
        route='/v1/responses' if native else '/anthropic/v1/messages'
        body=({'model':'test-model','reasoning':{'effort':'max'},'input':'Hi','max_output_tokens':100}
              if native else {'model':'r','messages':[],'max_tokens':100})
        response=client.post(route,json=body,headers={'x-api-key':'test'})
    assert response.status_code==402
    assert life.poll()=='researcher_budget_limit'
    assert life.evidence['wallet']=='designer' and life.evidence['reservation_usd']>entry['cap']
    wallet=app.state.ledger.status('designer')[0]
    assert wallet['calls']==0 and wallet['cap']==entry['cap']
    assert life.select()['sequence']==1


def test_candidate_budget_or_old_designer_denial_does_not_stop_research(tmp_path):
    life,work,out=lifecycle(tmp_path);submission(work)
    for ident,wallet,created in [('candidate','development',time.time()),('old','designer',life.started-1)]:
        p=out/'gateway/wire'/ident;p.mkdir(parents=True)
        (p/'meta.json').write_text(json.dumps({'id':ident,'wallet':wallet,'model':'r','created':created,'state':'rejected_budget'}))
    assert life.poll() is None


@pytest.mark.parametrize('native',[False,True])
def test_gateway_deadline_before_harness_exit_is_an_expected_stop(tmp_path,native):
    life,work,out=lifecycle(tmp_path);submission(work);life.capture(force=True)
    life.deadline=time.time()-1
    key=tmp_path/'key';key.write_text('test-only')
    entry={'wallet':'designer','cap':1,'models':['r'],'deadline_epoch':life.deadline}
    config={**life.config,'key_file':str(key),'upstream':'http://127.0.0.1:9',
            'prices':{'r':{'input':1,'output':1}},'tokens':{'test':entry},
            'model_backends':{'r':{'model':'test-model','key_file':str(key),'upstream':'http://127.0.0.1:9/v1'}}}
    if native:
        entry['native_responses']=True
        config['native_researcher']={'id':'r','model':'test-model','effort':'max','max_output_tokens':100,'max_context_tokens':1000}
    app=create_app(config)
    with TestClient(app) as client:
        response=client.post('/v1/responses' if native else '/anthropic/v1/messages',json={},headers={'x-api-key':'test'})
    assert response.status_code==409
    outcome=life.finish(tmp_path,1,harness='codex' if native else 'claude_code')
    assert outcome['expected_resource_stop'] and outcome['reason']=='research_time_limit'
    assert outcome['evidence']['path'].endswith('.json')
    assert len(life.entries)==1 and app.state.ledger.status('designer')[0]['calls']==0


def test_short_legacy_checkpoint_timeout_is_distinct_from_total_deadline(tmp_path):
    life,work,_=lifecycle(tmp_path);submission(work);life.capture(force=True)
    trace=tmp_path/'trace';trace.mkdir()
    rc=run_logged([sys.executable,'-c','import time;time.sleep(30)'],trace/'claude',timeout=.05)
    outcome=life.finish(trace,rc,harness='claude_code')
    assert outcome['expected_resource_stop'] and outcome['reason']=='checkpoint_time_limit'
    assert time.time()<life.deadline


@pytest.mark.parametrize('cause',['upstream_blocked','researcher_accounting_guard'])
def test_policy_or_accounting_stop_is_not_ordinary_budget_exhaustion(tmp_path,cause):
    life,work,out=lifecycle(tmp_path);submission(work)
    ledger=Ledger(out/'gateway/ledger.sqlite');ledger.wallet('designer',0 if cause=='researcher_accounting_guard' else 1)
    if cause=='upstream_blocked':
        (out/'gateway/native-upstream-blocked.json').write_text(json.dumps({'code':'cyber_policy','at':time.time()}))
    assert life.poll()==cause
    outcome=life.finish(tmp_path,125,harness='codex')
    assert outcome['reason']==cause and not outcome['expected_resource_stop']
    assert life.select()['sequence']==1


def test_host_timeout_preserves_predeadline_snapshot_and_records_real_termination(tmp_path):
    life,work,_=lifecycle(tmp_path);submission(work);life.capture(force=True)
    trace=tmp_path/'trace';trace.mkdir()
    rc=run_logged([sys.executable,'-c','import time;time.sleep(30)'],trace/'claude',timeout=.05,stop_requested=life.poll)
    assert rc==124
    life.deadline=time.time()-1
    submission(work,'late-after-timeout')
    outcome=life.finish(trace,rc,harness='claude_code')
    assert outcome['reason']=='research_time_limit' and outcome['expected_resource_stop']
    assert "VERSION='first'" in (Path(life.select()['path'])/'predictor.py').read_text()
    info=json.loads((trace/'claude.process.json').read_text())
    with pytest.raises(ProcessLookupError):os.kill(info['pid'],0)
    # A return code or model-written claim alone does not prove a host timeout.
    other=tmp_path/'other';other.mkdir()
    life.reason=None;life.evidence=None
    assert not life.finish(other,124,harness='claude_code')['expected_resource_stop']


def test_host_stops_child_on_controller_budget_signal(tmp_path):
    started=time.monotonic()
    def stop():return 'researcher_budget_limit' if time.monotonic()-started>.05 else None
    rc=run_logged([sys.executable,'-c','import time;time.sleep(30)'],tmp_path/'claude',timeout=10,stop_requested=stop)
    assert rc==125
    info=json.loads((tmp_path/'claude.process.json').read_text())
    assert info['termination_reason']=='researcher_budget_limit'
    with pytest.raises(ProcessLookupError):os.kill(info['pid'],0)


@pytest.mark.parametrize('harness',['claude_code','codex'])
def test_bare_timeout_exit_code_cannot_substitute_for_host_evidence(tmp_path,harness):
    with pytest.raises(RuntimeError):check_designer_exit(tmp_path,124,harness=harness)


@pytest.mark.parametrize('unknown',[False,True])
def test_joint_budget_stop_accepts_saved_suite_reuses_jobs_and_keeps_unknown_cost(tmp_path,monkeypatch,unknown):
    root=os.environ.get('SEB_TEST_ROOT');science=os.environ.get('SEB_TEST_SCIENCE')
    if not root or not science:pytest.skip('Set sandbox paths for joint integration')
    import seb.experiment as experiment
    path,cfg=joint_fixture(tmp_path)
    cfg['runtime']={'rootfs':root,'science_packages':science}
    cfg['budgets']['researcher_usd']=.000001
    path.write_text(yaml.safe_dump(cfg))
    lives=[];created_jobs=[]
    def make_lifecycle(*a,**kw):
        value=ResearchLifecycle(*a,**kw);lives.append(value);return value
    monkeypatch.setattr(experiment,'ResearchLifecycle',make_lifecycle)
    def research(work):
        submission(work)
        p=work/'submission/evaluation.json';manifest=json.loads(p.read_text())
        manifest['domain_aggregations']={d:{'kind':'weighted_mean','weights':{'sum':1,'product':1}}
                                         for d in ['coding','co-work','reasoning']}
        p.write_text(json.dumps(manifest))
        life=lives[0];life.capture(force=True)
        config=json.loads((work.parent/'gateway.private.json').read_text())
        token=next(t for t,e in config['tokens'].items() if e['wallet']=='development')
        # Real isolated suite executions through the local fixture provider.
        rows=experiment.run_jobs(config,token,['dev-a','dev-b','dev-c'],'submission',work.parent/'already-tested',life.deadline)
        assert all(r['score_status']=='valid' for r in rows)
        created_jobs.extend(r['job_id'] for r in rows)
        if unknown:
            ledger=Ledger(work.parent/'gateway/ledger.sqlite')
            ledger.reserve('unknown-fixture','designer','researcher-a',.0000009)
            ledger.finish('unknown-fixture',None,{},'transport_unknown')
        designer=next(t for t,e in config['tokens'].items() if e['wallet']=='designer')
        with pytest.raises(RuntimeError,match='HTTP 402'):
            experiment.request(config['gateway_socket'],designer,'/anthropic/v1/messages',
                               {'model':'researcher-a','messages':[],'max_tokens':100})
        # Research has a partially written next revision when its D allowance stops it.
        (work/'submission/run.py').write_text('unfinished(')
    monkeypatch.setattr(experiment,'mock_submission',research)
    out=tmp_path/'joint-stop';result=experiment.run(path,None,out,mock=True)
    assert result['research_outcome']['reason']=='researcher_budget_limit'
    assert result['research_outcome']['expected_resource_stop']
    assert result['eligible'] is (not unknown)
    assert result['accounting']['within_budget']
    assert result['accounting']['unknown_calls']==int(unknown)
    assert result['complete_models']==result['expected_models']==3
    assert len(result['domains'])==3
    assert (out/'researcher-work/submission/run.py').read_text()=='unfinished('
    assert 'unfinished(' not in (out/'acceptance-input/suite/run.py').read_text()
    dev=json.loads((out/'development-1/results.json').read_text())
    assert {r['job_id'] for r in dev}==set(created_jobs)
    with Ledger(out/'gateway/ledger.sqlite').connect() as db:
        assert db.execute("SELECT COUNT(*) FROM calls WHERE wallet='development'").fetchone()[0]==6
        assert db.execute("SELECT COUNT(*) FROM calls WHERE wallet='evaluation'").fetchone()[0]==6
        assert db.execute("SELECT cap FROM wallets WHERE name='designer'").fetchone()[0]==.000001
        if unknown:assert db.execute("SELECT charged FROM calls WHERE id='unknown-fixture'").fetchone()[0] is None
    report=json.loads((out/'domain/scores.json').read_text())
    assert len(report['visible'])==len(report['sealed'])==6
    assert not (out/'researcher-work/access.json').exists()
