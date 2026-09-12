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
