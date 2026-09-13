import json
import pytest
from seb.domain_scoring import summarize_outputs, score_domain


def data():
    models=[{'id':'dev','family':'a','split':'development'}]+[
        {'id':f'h{i}','family':f'f{i//2}','split':'holdout'} for i in range(8)]
    refs={t:{'dev':9999,**{f'h{i}':i if t=='visible' else 7-i for i in range(8)}} for t in ['visible','sealed']}
    preds={f'h{i}':{'visible':i} for i in range(8)}
    z={f'h{i}':i for i in range(8)}
    return models,refs,preds,z


def test_sealed_cannot_refit_or_flip_sign_and_dev_does_not_enter_score():
    models,refs,preds,z=data()
    r=summarize_outputs(models,refs,['visible'],['sealed'],preds,z)
    assert r['visible_utility']==1 and r['sealed_utility']==-1
    assert 'dev' not in r['acceptance_models'] and r['fit_uses_sealed_labels'] is False
    refs['sealed']['dev']=-1e20
    assert summarize_outputs(models,refs,['visible'],['sealed'],preds,z)==r


def test_missing_outputs_are_penalized_but_reference_gaps_are_pending():
    models,refs,preds,z=data();del preds['h0']
    r=summarize_outputs(models,refs,['visible'],['sealed'],preds,z)
    assert r['visible_utility']==-1 and r['visible']['visible']['status']=='FAIL'
    del refs['sealed']['h0']
    r=summarize_outputs(models,refs,['visible'],['sealed'],preds,z)
    assert r['sealed_utility'] is None and r['sealed']['sealed']['status']=='PENDING_REFERENCE'


def test_constant_is_explicit_and_never_nan():
    models,refs,preds,z=data();z={k:.5 for k in z}
    r=summarize_outputs(models,refs,['visible'],['sealed'],preds,z)
    assert r['sealed_utility']==0 and r['sealed']['sealed']['spearman'] is None
    assert r['sealed']['sealed']['status']=='CONST'
    json.dumps(r,allow_nan=False)


def test_submitted_code_receives_visible_dev_labels_only(tmp_path,monkeypatch):
    models,refs,preds,z=data();source=tmp_path/'source';source.mkdir()
    (source/'evaluation.json').write_text('{"items":[{"id":"x"}]}')
    (source/'predictor.py').write_text('# fixture')
    def result(mid,val):
        return {'model':mid,'score_status':'valid','result':{'score':val,'items':[{'id':'x','score':val,'execution_status':'completed'}]}}
    observed=[]
    def isolated(config,submitted,payload,folder):
        observed.append(payload)
        assert set(payload['targets'])=={'visible'}
        assert payload['training']==[{'observations':{'x':0},'targets':{'visible':9999}}]
        encoded=json.dumps(payload)
        assert 'sealed' not in encoded and 'h0' not in encoded and 'family' not in encoded
        return [{'visible':{'score':i}} for i in range(8)]
    monkeypatch.setattr('seb.domain_scoring.isolated',isolated)
    report=score_domain({},source,[result('dev',0)],[result(f'h{i}',i) for i in range(8)],models,refs,
        {'visible':{'scale':10000}}, {'sealed':{'scale':10000}},tmp_path/'output')
    assert len(observed)==1 and report['sealed_utility']==-1 and report['visible_utility']==1
