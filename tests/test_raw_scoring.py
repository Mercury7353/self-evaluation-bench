import json
import pytest
from seb.scoring import score_panel
from seb.domain_scoring import score_domain

def test_raw_scores_bypass_predictor_and_preserve_ties(tmp_path,monkeypatch):
    source=tmp_path/'suite';source.mkdir()
    # Deliberately no predictor.py: visible and sealed must both use item grades.
    manifest={'items':[{'id':'x'},{'id':'y'}], 'domain_aggregations':{'coding':{'kind':'weighted_mean','weights':{'x':1}},'co-work':{'kind':'weighted_mean','weights':{'y':1}}}}
    (source/'evaluation.json').write_text(json.dumps(manifest))
    models=[{'id':str(i),'family':str(i),'split':'holdout'} for i in range(3)]
    results=[{'model':str(i),'score_status':'valid','result':{'items':[{'id':'x','score':i/2,'execution_status':'completed'},{'id':'y','score':1,'execution_status':'completed'}]}} for i in range(3)]
    refs={'a':{'0':0,'1':1,'2':2},'b':{'0':2,'1':1,'2':0},'sealed':{'0':2,'1':1,'2':0}}
    targets={'a':{'domain':'coding'},'b':{'domain':'co-work'}}
    def forbidden(*a,**k):raise AssertionError('Predictor executed')
    monkeypatch.setattr('seb.scoring.fitted_cv',forbidden);monkeypatch.setattr('seb.domain_scoring.isolated',forbidden)
    report=score_panel({'score_mode':'raw_domain'},source,results,models,refs,targets,tmp_path/'development')
    assert report['targets']['a']['spearman']==1
    assert report['targets']['b']['status']=='CONST' and report['overall']['score']==.5
    final=score_domain({'score_mode':'raw_domain'},source,[],results,models,refs,targets,{'sealed':{}},tmp_path/'acceptance',minimum_models=3,minimum_families=2,domains={'coding':{'visible':['a'],'sealed':['sealed']},'co-work':{'visible':['b'],'sealed':[]}})
    assert final['visible']['a']['spearman']==1 and final['sealed']['sealed']['spearman']==-1
    assert final['predictor_error'] is None and 'predictions' not in final
