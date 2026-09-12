import copy

from seb.reference_panel import audit_panel, compare, digest


def panel():
    models=[{'id':f'c{i}','family':f'f{i//2}','checkpoint':f'v{i}','provider':'test'} for i in range(6)]
    protocol={'dataset_version':'test-v1','dataset_digest':'test-digest','harness':'test',
              'harness_commit':'test-commit','failure_policy':'model_failure_zero','resource_limits':{'seconds':1}}
    target={'id':'visible','visibility':'visible','task_ids':['one','two'],'repeats':1,'execution_protocol':protocol,
            'results':{m['id']:{'protocol_digest':digest(protocol),'source':'fixture','artifact_sha256':'fixture',
                       'trials':[{'task_id':t,'repeat':0,'score':i/5,'status':'completed'} for t in ['one','two']]}
                       for i,m in enumerate(models)}}
    hidden=copy.deepcopy(target);hidden.update(id='hidden',visibility='hidden')
    for row in hidden['results'].values():
        for trial in row['trials']:trial['score']=1-trial['score']
    return {'protocol_version':1,'models':models,'targets':[target,hidden]}


def test_same_frozen_proxy_compared_to_visible_and_hidden_targets():
    reference=panel();proxy=[{'model':f'c{i}','score':i/5,'score_status':'valid','accounting_status':'pending'} for i in range(6)]
    result=compare(reference,proxy,bootstrap_samples=30)
    assert result['status']=='exploratory'
    assert result['targets']['visible']['spearman']==1
    assert result['targets']['hidden']['spearman']==-1
    assert result['targets']['visible']['within_family']['f0']['pairs']['concordant']==1
    assert result==compare(reference,proxy,bootstrap_samples=30)


def test_missing_trials_protocol_mismatch_and_infra_are_not_silently_scored():
    data=panel();data['targets'][0]['results']['c0']['trials'].pop()
    assert not audit_panel(data)['ready']
    data=panel();data['targets'][0]['results']['c0']['protocol_digest']='other-harness'
    assert not audit_panel(data)['ready']
    data=panel();data['targets'][0]['results']['c0']['trials'][0]['status']='infra_error'
    assert not audit_panel(data)['ready']


def test_no_cherry_picking_models_or_invalid_proxy_results():
    proxy=[{'model':f'c{i}','score':i/5,'score_status':'valid'} for i in range(6)]
    assert compare(panel(),proxy[:-1])['status']=='incomplete'
    proxy[-1]['score_status']='incomplete'
    assert compare(panel(),proxy)['status']=='incomplete'
