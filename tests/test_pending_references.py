import json
import os
from pathlib import Path
import sqlite3

import pytest
import yaml

from seb.domain_scoring import summarize_outputs, reference_status
from seb.experiment_config import load, researcher_view
from seb.reference_update import rescore
from test_domain_scoring import data
from test_experiment import joint_fixture


def pending_config(tmp_path):
    path,cfg=joint_fixture(tmp_path)
    cfg['domain_protocol']['allow_pending_references']=True
    held=[m['id'] for m in cfg['models'] if m['split']=='holdout']
    empty=tmp_path/'missing-sealed.json';empty.write_text('{}')
    for visibility in ('whitebox','blackbox'):
        for target in cfg['benchmarks'][visibility]:
            target.update(holdout_panel=held,reference_pending_reason='source-package-pending-private')
            if visibility=='blackbox':target['reference']=str(empty)
    visible=cfg['benchmarks']['whitebox'][0]
    refs=json.loads(Path(visible['reference']).read_text());refs.pop(held[0])
    partial=tmp_path/'partial-visible.json';partial.write_text(json.dumps(refs));visible['reference']=str(partial)
    path.write_text(yaml.safe_dump(cfg))
    return path,cfg


def test_pending_acceptance_references_keep_panels_without_exposing_them(tmp_path):
    path,cfg=pending_config(tmp_path);loaded=load(path)
    assert len(loaded['_pending_reference_targets'])==7 and len(loaded['_reference_panels'])==12
    assert all(len(p)==3 for p in loaded['_reference_panels'].values())
    view=json.dumps(researcher_view(loaded))
    assert 'holdout' not in view and 'blackbox' not in view and 'source-package-pending-private' not in view
    assert len(researcher_view(loaded)['targets'])==6
    cfg['domain_protocol'].pop('allow_pending_references');path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError,match='Insufficient frozen holdout reference'):load(path)


@pytest.mark.parametrize('mutation',['missing_panel','dev_in_panel','small_panel','duplicate_panel','missing_reason','missing_development','invalid_score'])
def test_pending_mode_does_not_relax_development_or_panel_validity(tmp_path,mutation):
    path,cfg=pending_config(tmp_path)
    sealed=cfg['benchmarks']['blackbox'][0];visible=cfg['benchmarks']['whitebox'][0]
    if mutation=='missing_panel':sealed.pop('holdout_panel')
    elif mutation=='dev_in_panel':sealed['holdout_panel']=['dev-a','holdout-a','holdout-b']
    elif mutation=='small_panel':sealed['holdout_panel']=['holdout-a']
    elif mutation=='duplicate_panel':sealed['holdout_panel']=['holdout-a']*3
    elif mutation=='missing_reason':sealed.pop('reference_pending_reason')
    else:
        refs=json.loads(Path(visible['reference']).read_text())
        if mutation=='missing_development':refs.pop('dev-a')
        else:refs['holdout-a']='not a score'
        Path(visible['reference']).write_text(json.dumps(refs))
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError):load(path)


def test_reference_gap_cannot_shrink_a_frozen_panel_that_still_exceeds_minimum():
    models,refs,preds,z=data()
    models.append({'id':'h8','family':'f4','split':'holdout'})
    preds['h8']={'visible':8};z['h8']=8
    panels={t:[m['id'] for m in models if m['split']=='holdout'] for t in refs}
    # Eight known rows pass the old minimum; the declared nine-model panel still lacks one.
    result=summarize_outputs(models,refs,['visible'],['sealed'],preds,z,reference_panels=panels)
    assert result['visible']['visible']['status']=='PENDING_REFERENCE'
    assert result['visible']['visible']['n_reference']==8
    assert result['visible']['visible']['missing_reference_models']==['h8']
    assert result['visible_utility'] is None
    refs['visible']['h8']=8;refs['sealed']['h8']=-1
    del preds['h0']
    result=summarize_outputs(models,refs,['visible'],['sealed'],preds,z,reference_panels=panels)
    assert result['visible']['visible']['status']=='FAIL' and result['visible_utility']==-1
    assert result['sealed_utility']==-1
    refs['sealed'].pop('h8')
    result=summarize_outputs(models,refs,['visible'],['sealed'],preds,z,reference_panels=panels)
    state=reference_status(result)
    assert state['reference_status']=='pending' and state['has_submission_failure']
    assert state['failed_target_outputs']==['visible']


def frozen_input(tmp_path):
    models,refs,preds,z=data()
    refs['sealed'].pop('h0')
    frozen={'version':1,'models':models,'references':refs,'visible_targets':['visible'],
        'sealed_targets':['sealed'],'domains':None,'reference_panels':{t:[f'h{i}' for i in range(8)] for t in refs},
        'predictions':preds,'domain_scores':z,'minimum_models':8,'minimum_families':4}
    path=tmp_path/'input.json';path.write_text(json.dumps(frozen))
    additions={'version':1,'scores':{'sealed':{'h0':7}},
        'evidence':{'sealed':{'h0':{'source':'fixture://complete-source','sha256':'a'*64}}}}
    extra=tmp_path/'additions.json';extra.write_text(json.dumps(additions))
    return path,extra,frozen,additions


@pytest.mark.parametrize('mutation',['replace_score','add_candidate','add_target','edit_predictions','missing_evidence','invalid_score'])
def test_reference_additions_cannot_rewrite_results_or_panels(tmp_path,mutation):
    path,extra,_,change=frozen_input(tmp_path)
    if mutation=='replace_score':change['scores']['sealed']={'h1':6}
    elif mutation=='add_candidate':change['scores']['sealed']={'other':1}
    elif mutation=='add_target':change['scores']={'new-target':{'h0':7}}
    elif mutation=='edit_predictions':change['predictions']={'h0':1}
    elif mutation=='missing_evidence':change['evidence']['sealed']['h0']['sha256']='missing'
    else:change['scores']['sealed']['h0']=None
    if mutation in ('replace_score','add_candidate','add_target'):
        change['evidence']={t:{m:{'source':'fixture://source','sha256':'a'*64} for m in scores}
                            for t,scores in change['scores'].items()}
    extra.write_text(json.dumps(change));output=tmp_path/'rescore'
    with pytest.raises(ValueError):rescore(path,extra,output)
    assert not output.exists()


def test_new_labels_reuse_saved_predictions_without_fitting_or_overwriting(tmp_path,monkeypatch):
    path,extra,_,_=frozen_input(tmp_path);before=path.read_bytes()
    def forbidden(*args,**kwargs):raise AssertionError('No predictor or provider execution during reference addition')
    monkeypatch.setattr('seb.domain_scoring.isolated',forbidden)
    result=rescore(path,extra,tmp_path/'rescore')
    assert result['reference_status']=='ready' and result['visible_utility']==1 and result['sealed_utility']==-1
    assert result['new_model_calls']==result['new_predictor_fits']==0
    assert path.read_bytes()==before
    with pytest.raises(FileExistsError):rescore(path,extra,tmp_path/'rescore')


def test_joint_research_finishes_with_pending_references_then_scores_without_calls(tmp_path,monkeypatch):
    root=os.environ.get('SEB_TEST_ROOT');science=os.environ.get('SEB_TEST_SCIENCE')
    if not root or not science:pytest.skip('Set sandbox paths for pending-reference integration')
    import seb.experiment as experiment
    path,cfg=pending_config(tmp_path);cfg['runtime']={'rootfs':root,'science_packages':science}
    original=experiment.mock_submission
    def submission(work):
        public='\n'.join((work/n).read_text() for n in ('whitebox.json','access.json','CONTRACT.md'))
        assert 'holdout' not in (work/'whitebox.json').read_text()
        assert 'source-package-pending-private' not in public
        original(work)
        manifest=json.loads((work/'submission/evaluation.json').read_text())
        manifest['domain_aggregations']={d:{'kind':'weighted_mean','weights':{'sum':1,'product':1}}
            for d in cfg['domain_protocol']['domains']}
        (work/'submission/evaluation.json').write_text(json.dumps(manifest))
        (work/'submission/predictor.py').write_text('''
def fit(training_rows,target_metadata):
    assert len(target_metadata)==6 and all('whitebox' in t for t in target_metadata)
    return list(target_metadata)
def predict(fitted,observations):
    return {t:sum(observations.values())/len(observations) for t in fitted}
''')
    monkeypatch.setattr(experiment,'mock_submission',submission)
    path.write_text(yaml.safe_dump(cfg));out=tmp_path/'run'
    result=experiment.run(path,None,out,mock=True)
    assert result['measurement_complete'] and result['complete_models']==result['expected_models']==3
    assert result['reference_status']=='pending' and len(result['pending_reference_targets'])==7
    assert not result['eligible'] and result['visible_utility'] is None and result['sealed_utility'] is None
    assert json.loads((out/'state.json').read_text())['phase']=='pending_reference'
    scores=json.loads((out/'domain/scores.json').read_text())
    assert len(scores['visible'])==len(scores['sealed'])==6
    scoring=out/'domain/reference-scoring-input.json';frozen=json.loads(scoring.read_text())
    missing={t:{m:i/2 for i,m in enumerate(panel) if m not in frozen['references'][t]}
        for t,panel in frozen['reference_panels'].items()}
    missing={t:v for t,v in missing.items() if v}
    additions={'version':1,'scores':missing,'evidence':{t:{m:{'source':'fixture://'+t,'sha256':'b'*64}
        for m in values} for t,values in missing.items()}}
    extra=tmp_path/'additions.json';extra.write_text(json.dumps(additions))
    def ledger_rows():
        with sqlite3.connect((out/'gateway/ledger.sqlite').as_uri()+'?mode=ro',uri=True) as db:
            return db.execute('SELECT * FROM calls ORDER BY id').fetchall()
    before=ledger_rows();original_result=(out/'result.json').read_bytes();original_scores=(out/'domain/scores.json').read_bytes()
    assert len(before)==12
    def forbidden(*args,**kwargs):raise AssertionError('Frozen predictor must not refit')
    monkeypatch.setattr('seb.domain_scoring.isolated',forbidden)
    updated=rescore(scoring,extra,tmp_path/'reference-completed')
    assert updated['reference_status']=='ready' and updated['visible_utility']==updated['sealed_utility']==1
    assert updated['predictions']==frozen['predictions'] and updated['domain_scores']==frozen['domain_scores']
    assert updated['new_model_calls']==updated['new_predictor_fits']==0
    assert ledger_rows()==before and (out/'result.json').read_bytes()==original_result
    assert (out/'domain/scores.json').read_bytes()==original_scores
