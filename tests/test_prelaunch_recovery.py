import json
import os
import time
from pathlib import Path
import pytest

from seb.ledger import Ledger
from seb.prelaunch_recovery import archive_unstarted,restore_ledger


def failed(tmp_path):
    output=tmp_path/'run';output.mkdir()
    started=time.time()-30
    (output/'state.json').write_text(json.dumps({'phase':'failed','researcher':'r','started':started}))
    (output/'provenance.json').write_text(json.dumps({'config_sha256':'frozen'}))
    cfg={'_config_sha256':'frozen','design':{'seconds':3600},'budgets':{'development_usd':100,'researcher_usd':200,'evaluation_usd':360}}
    ledger=Ledger(output/'gateway/ledger.sqlite')
    for name,cap in [('development',100),('designer',200),('evaluation',360)]:ledger.wallet(name,cap)
    return output,cfg,ledger,started


def test_unstarted_recovery_retains_history_wallets_and_original_deadline(tmp_path):
    output,cfg,ledger,started=failed(tmp_path)
    original_wallets=ledger.status()
    recovery=archive_unstarted(output,cfg,'r')
    assert recovery['original_started']==started and recovery['original_deadline_epoch']==started+3600
    assert not output.exists() and Path(recovery['archive']).is_dir()
    output.mkdir();restore_ledger(recovery,output)
    assert Ledger(output/'gateway/ledger.sqlite').status()==original_wallets
    with pytest.raises(ValueError,match='overwrite'):restore_ledger(recovery,output)


@pytest.mark.parametrize('mutation',['call','closed_wallet','wrong_config','expired','live_harness','live_state'])
def test_prelaunch_recovery_rejects_any_research_or_unverified_stop(tmp_path,mutation):
    output,cfg,ledger,_=failed(tmp_path)
    if mutation=='call':ledger.reserve('existing','designer','r',1)
    elif mutation=='closed_wallet':
        with ledger.connect() as db:db.execute("UPDATE wallets SET cap=0 WHERE name='designer'")
    elif mutation=='wrong_config':cfg['_config_sha256']='changed'
    elif mutation=='expired':cfg['design']['seconds']=1
    elif mutation=='live_harness':
        d=output/'researcher-trace-1';d.mkdir();(d/'codex.process.json').write_text('{}')
    else:
        p=output/'state.json';s=json.loads(p.read_text());s['phase']='designing';p.write_text(json.dumps(s))
    with pytest.raises(ValueError):archive_unstarted(output,cfg,'r')
    assert output.exists()


def test_actual_mock_recovery_keeps_original_clock_and_original_caps(tmp_path,monkeypatch):
    import sqlite3
    import yaml
    import seb.experiment as experiment
    from test_experiment import fixture_config
    root=os.environ.get('SEB_TEST_ROOT');science=os.environ.get('SEB_TEST_SCIENCE')
    if not root or not science:pytest.skip('Sandbox paths required')
    path,cfg=fixture_config(tmp_path)
    cfg['runtime']={'rootfs':root,'science_packages':science}
    path.write_text(yaml.safe_dump(cfg));out=tmp_path/'run'
    original=experiment.mock_submission;gateways=[];build=experiment.build_gateway
    def capture(*args,**kwargs):
        result=build(*args,**kwargs);gateways.append(result[0]);return result
    def fail_before_model(work):raise RuntimeError('Controlled fixture failure before first call')
    monkeypatch.setattr(experiment,'build_gateway',capture)
    monkeypatch.setattr(experiment,'mock_submission',fail_before_model)
    with pytest.raises(RuntimeError,match='Controlled fixture'):experiment.run(path,None,out,mock=True)
    first=json.loads((out/'state.json').read_text())
    with sqlite3.connect(out/'gateway/ledger.sqlite') as db:assert db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]==0
    monkeypatch.setattr(experiment,'mock_submission',original)
    result=experiment.run(path,None,out,mock=True,resume_prelaunch=True)
    state=json.loads((out/'state.json').read_text())
    assert state['started']==first['started']
    assert Path(state['prelaunch_recovery']['archive'],'state.json').exists()
    assert gateways[-1]['research_deadline_epoch']==first['started']+cfg['design']['seconds']
    assert all(e['deadline_epoch']==gateways[-1]['research_deadline_epoch'] for e in gateways[-1]['tokens'].values() if 'deadline_epoch' in e)
    assert {w['name']:w['cap'] for w in result['accounting']['wallets']}=={'development':cfg['budgets']['development_usd'],'evaluation':cfg['budgets']['evaluation_usd'],'designer':cfg['budgets']['researcher_usd']}
