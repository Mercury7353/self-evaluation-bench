import json
from pathlib import Path
import pytest
from seb.experiment_config import load
from seb.execution_policy import policy_for, DEFAULT_POLICY
from seb.evaluation import load_manifest
from seb.chat_candidate import CandidateAdapter
from seb.native_responses import usage_cost


def test_hundred_questions_cannot_be_one_batch(tmp_path):
    (tmp_path/'evaluation.json').write_text(json.dumps({'protocol_version':1,'items':[{'id':'batch'}],'aggregation':{'kind':'weighted_mean'}}))
    with pytest.raises(ValueError,match='100'):load_manifest(tmp_path,100)
    (tmp_path/'evaluation.json').write_text(json.dumps({'protocol_version':1,'items':[{'id':str(i),'budget_group':'batch'} for i in range(100)],'aggregation':{'kind':'weighted_mean'}}))
    assert len(load_manifest(tmp_path,100)['items'])==100


def test_small_output_is_explicit_config():
    cfg={'evaluation_policy':dict(DEFAULT_POLICY,min_output_tokens=1,default_output_tokens=8192)}
    assert policy_for(cfg)['min_output_tokens']==1


def test_researcher_discount_uses_cache_once():
    u={'input_tokens':1000,'output_tokens':100,'input_tokens_details':{'cached_tokens':900}}
    p={'input':10,'output':50,'cache_read_multiplier':.1}
    assert usage_cost(u,p,cached=True)==pytest.approx(.0069)
    assert usage_cost(u,p)==pytest.approx(.015)


def test_five_target_config_removes_tau_and_pools():
    path=Path('/nfs/hpc/share/zhanyaol/self-evaluation-artifacts/openai-mixed-20260915/config.yaml')
    if not path.exists():pytest.skip('operator configuration not available')
    c=load(path)
    assert len(c['benchmarks']['whitebox'])==5
    assert not c['design']['final_development_measurement']
    assert c['design']['minimum_items']==100
    assert all('auxiliary-pools' not in r for t in c['benchmarks']['whitebox'] for r in t['resources'])


def test_glm_unknown_usage_can_deliver_but_is_not_settled(tmp_path):
    from seb.gateway import cost
    backend={'model':'glm-5.2','effort':'max','allow_unreconciled_usage':True}
    response={'id':'probe','choices':[{'message':{'role':'assistant','content':'OK'},'finish_reason':'stop'}],
              'usage':{'prompt_tokens':6,'completion_tokens':4,'total_tokens':69}}
    assert cost(response['usage'],{'input':.75,'output':2.4}) is None
    adapter=CandidateAdapter(tmp_path,backend,'dev','test')
    adapter.translate(response,False)
    strict=CandidateAdapter(tmp_path,dict(backend,allow_unreconciled_usage=False),'dev','test')
    with pytest.raises(ValueError,match='ambiguous'):strict.translate(response,False)
