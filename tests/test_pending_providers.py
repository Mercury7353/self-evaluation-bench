import json
import os
import yaml
import pytest

from seb.experiment_config import load, researcher_view
from seb.experiment import build_gateway
from seb.domain_scoring import summarize_outputs, reference_status
from test_experiment import joint_fixture
from test_domain_scoring import data


def test_pending_candidate_keeps_panel_but_gets_no_gateway_route(tmp_path):
    path,cfg=joint_fixture(tmp_path)
    model=next(m for m in cfg['models'] if m['split']=='holdout')
    model.update(availability='pending',pending_reason='User deferred provider access')
    for key in ('provider','model','price'):model.pop(key,None)
    path.write_text(yaml.safe_dump(cfg));cfg=load(path)
    out=tmp_path/'out';out.mkdir()
    gateway,tokens=build_gateway(cfg,cfg['researchers'][0],out,tmp_path/'socket',mock_url='http://localhost:1')
    assert model['id'] not in gateway['model_backends']
    assert model['id'] not in gateway['prices']
    assert all(model['id'] not in role['models'] for role in gateway['tokens'].values())
    assert all(model['id'] in panel for panel in cfg['_reference_panels'].values())
    assert 'pending' not in json.dumps(researcher_view(cfg))


@pytest.mark.parametrize('bad',['development','researcher','missing_reason'])
def test_pending_cannot_hide_an_unavailable_research_input(tmp_path,bad):
    path,cfg=joint_fixture(tmp_path)
    item=cfg['researchers'][0] if bad=='researcher' else next(m for m in cfg['models'] if m['split']==('development' if bad=='development' else 'holdout'))
    item.update(availability='pending',pending_reason='' if bad=='missing_reason' else 'unavailable')
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError):load(path)


def test_pending_does_not_shrink_panel_or_mask_available_candidate_failure():
    models,refs,predictions,z=data()
    held=next(m for m in models if m['id']=='h0')
    held.update(availability='pending',pending_reason='User deferred provider access')
    del predictions['h0'];del z['h0']
    report=summarize_outputs(models,refs,['visible'],['sealed'],predictions,z)
    assert report['visible']['visible']['status']=='PENDING_PROVIDER'
    assert len(report['visible']['visible']['expected_models'])==8
    assert report['visible_utility'] is None and not reference_status(report)['has_submission_failure']
    del predictions['h1']
    report=summarize_outputs(models,refs,['visible'],['sealed'],predictions,z)
    assert report['visible']['visible']['status']=='FAIL'
    assert report['visible']['visible']['missing_models']==['h1']
    assert report['sealed']['sealed']['status']=='PENDING_PROVIDER'
    assert reference_status(report)['has_submission_failure']


def test_joint_run_measures_available_models_once_and_stays_pending(tmp_path,monkeypatch):
    import seb.experiment as experiment
    root=os.environ.get('SEB_TEST_ROOT');science=os.environ.get('SEB_TEST_SCIENCE')
    if not root or not science:pytest.skip('Sandbox paths required')
    path,cfg=joint_fixture(tmp_path);cfg['runtime']={'rootfs':root,'science_packages':science}
    model=next(m for m in cfg['models'] if m['split']=='holdout')
    model.update(availability='pending',pending_reason='User deferred provider access')
    for key in ('provider','model','price'):model.pop(key,None)
    original=experiment.mock_submission
    def submission(work):
        original(work)
        manifest=json.loads((work/'submission/evaluation.json').read_text())
        manifest['domain_aggregations']={d:{'kind':'weighted_mean','weights':{'sum':1,'product':1}} for d in cfg['domain_protocol']['domains']}
        (work/'submission/evaluation.json').write_text(json.dumps(manifest))
        (work/'submission/predictor.py').write_text('def fit(rows,targets):return list(targets)\ndef predict(fitted,observations):return {t:sum(observations.values())/len(observations) for t in fitted}\n')
    monkeypatch.setattr(experiment,'mock_submission',submission)
    path.write_text(yaml.safe_dump(cfg));out=tmp_path/'run'
    result=experiment.run(path,None,out,mock=True)
    assert result['expected_models']==3 and result['complete_models']==2
    assert result['available_measurement_complete'] and not result['measurement_complete']
    assert not result['eligible'] and not result['has_submission_failure']
    assert json.loads((out/'state.json').read_text())['phase']=='pending_provider'
    assert len(json.loads((out/'acceptance-jobs/results.json').read_text()))==2
    import sqlite3
    with sqlite3.connect(out/'gateway/ledger.sqlite') as db:
        assert db.execute('SELECT COUNT(*) FROM calls WHERE model=?',(model['id'],)).fetchone()[0]==0
