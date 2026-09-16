import hashlib
import json
import time
import pytest
from seb.ledger import Ledger
from seb.measurement_resume import collect, fingerprint
from seb.research_sdk import Client
from seb.evaluation import normalize_result
from seb.evidence import check_budget_evidence


def test_resume_preserves_wrong_answer_and_replays_ungraded_response(tmp_path,monkeypatch):
    root=tmp_path;gateway=root/'gateway';ledger=Ledger(gateway/'ledger.sqlite');ledger.wallet('development',10);ledger.wallet('evaluation',10)
    job='a'*32;folder=root/'research-jobs'/job;source=folder/'submitted';source.mkdir(parents=True)
    manifest={'protocol_version':1,'items':[{'id':'q1'},{'id':'q2'}],'aggregation':{'kind':'weighted_mean'}}
    (source/'evaluation.json').write_text(json.dumps(manifest));(source/'run.py').write_text('pass');(source/'README.md').write_text('fixture')
    before=time.time()-1
    (folder/'request.json').write_text(json.dumps({'kind':'suite','model':'m','wallet':'development','created':before,'submission_id':fingerprint(source)}))
    oldrow=None
    for index,item in enumerate(['q1','q2'],1):
        call=str(index)*32;op='op'+str(index);group='item:'+job+':'+hashlib.sha256(item.encode()).hexdigest()
        ledger.reserve(call,'development','m',.1,scopes={'suite:'+job:5,group:1});ledger.finish(call,None if index==2 else .01,{},'completed')
        wire=gateway/'wire'/call;wire.mkdir(parents=True)
        body={'model':'m','max_tokens':2048,'messages':[{'role':'user','content':item}]}
        (wire/'request.body').write_text(json.dumps(body));(wire/'meta.json').write_text(json.dumps({'operation_id':op,'cache_sample_path':[]}))
        operation=gateway/'operations'/hashlib.sha256(('development\0'+op).encode()).hexdigest();operation.mkdir(parents=True)
        (operation/'result.json').write_text(json.dumps({'http_status':200,'headers':{'x-seb-request-id':call},'attempts':[{'id':call}]}))
        (operation/'response.body').write_text(json.dumps({'content':[{'type':'text','text':'answer'}],'stop_reason':'end_turn'}))
        if index==1:oldrow={'id':item,'score':0,'execution_status':'completed','answer_status':'answered','evidence':[{'kind':'llm','id':call}]}
    (folder/'result.json').write_text(json.dumps({'status':'error','result':{'items':[oldrow]}}))
    config={'artifacts':str(gateway),'run_root':str(root)};state=collect(config,source,'m','b'*32)
    assert state['prior_encumbered_usd']==pytest.approx(.11) # unknown original reserve retained
    ctx=root/'context';ctx.write_text(json.dumps({'model':'m','token':'fixture','output_dir':str(root/'raw'),'measurement_resume':state}))
    client=Client(ctx);monkeypatch.setattr(client,'request',lambda *a,**k:pytest.fail('Repeated candidate request'))
    assert client.item('q1','q1',lambda text:1)==oldrow # cannot regrade a completed wrong answer
    second=client.item('q2','q2',lambda text:1);assert second['score']==1 and second['evidence'][0]['id']=='2'*32
    raw={'protocol_version':1,'items':[oldrow,second]}
    with pytest.raises(ValueError,match='outside'):normalize_result(raw,manifest,ledger,'evaluation',time.time(),root/'research-jobs',candidate_model='m')
    result=normalize_result(raw,manifest,ledger,'evaluation',time.time(),root/'research-jobs',candidate_model='m',prior_evidence=state['prior_evidence'])
    assert result['score']==.5
    check_budget_evidence(result,manifest,ledger,'b'*32,root/'research-jobs',prior_evidence=state['prior_evidence'])
    assert ledger.status('evaluation')[0]['calls']==0
    assert collect(config,source,'another-model','b'*32)['completed_items']=={}
    (source/'run.py').write_text('print("changed version")')
    assert collect(config,source,'m','b'*32)['completed_items']=={}


def test_sdk_replays_completed_agent_instead_of_relaunch(tmp_path,monkeypatch):
    from seb.measurement_resume import replay_key
    body={'path':'tasks/demo','model':'m'};key=replay_key({'agent':body},'q')
    result={'id':'a'*32,'status':'ok','result':{'reward':1}}
    context={'model':'m','token':'fixture','output_dir':str(tmp_path/'raw'),'measurement_resume':{'agents':{key:{'id':'a'*32,'status':'ok'}},'agent_results':{'a'*32:result}}}
    p=tmp_path/'context';p.write_text(json.dumps(context));client=Client(p)
    monkeypatch.setattr(client,'request',lambda *a,**k:pytest.fail('Repeated agent request'))
    assert client.agent('/workspace/tasks/demo',item_id='q')['result']==result
