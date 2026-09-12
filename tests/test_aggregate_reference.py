import pytest
from seb.aggregate_reference import validate,statistics


def test_public_aggregate_pilot_is_explicit_and_withheld_scores_are_separate():
    ids=['a','b','c','d','e','f']
    panel={'mode':'exploratory_aggregate','source_sha256':'frozen','development_models':ids[:3],
           'evaluation_order':ids,'models':[{'id':m,'family':m} for m in ids],
           'targets':[{'id':'tb','caveat':'Heterogeneous historical protocols',
                       'rows':[{'model':m,'score':i/6} for i,m in enumerate(ids)]}]}
    rows=[{'model':m,'score':i/6,'score_status':'valid'} for i,m in enumerate(ids)]
    result=statistics(panel,rows)
    assert result['targets']['tb']['groups']['withheld']['n']==3
    assert result['targets']['tb']['groups']['withheld']['pearson']==pytest.approx(1)
    rows[-1].update(score=None,score_status='incomplete')
    result=statistics(panel,rows)
    assert result['missing_models']==['f']
    assert result['targets']['tb']['groups']['withheld']['pearson'] is None
    panel['mode']='formal'
    with pytest.raises(ValueError,match='explicit'):validate(panel)


def test_summation_noise_cannot_create_rank_differences():
    ids=['a','b','c','d','e','f']
    refs=[.634,.618,.721,.827,.65808,.746]
    scores=[34/36,35/36,1.,.9722222222222223,34/36,.75]
    panel={'mode':'exploratory_aggregate','source_sha256':'frozen','development_models':ids[:3],
           'evaluation_order':ids,'models':[{'id':m,'family':m} for m in ids],
           'targets':[{'id':'tb','caveat':'Fixture', 'rows':[{'model':m,'score':v} for m,v in zip(ids,refs)]}]}
    rows=[{'model':m,'score':v,'score_status':'valid'} for m,v in zip(ids,scores)]
    result=statistics(panel,rows)['targets']['tb']['groups']['all']
    assert result['spearman']==pytest.approx(-.029424494316824982)
    assert [p['score'] for p in result['pairs']]==scores
    for row in rows:row['score']=.9722222222222223 if row['model']=='a' else 35/36
    result=statistics(panel,rows)['targets']['tb']['groups']['all']
    assert result['spearman'] is None and result['status']=='insufficient_or_constant_scores'
