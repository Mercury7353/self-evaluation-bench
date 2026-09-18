import hashlib,json
from unittest.mock import patch
from seb.random_run import main
from seb.random_baseline import bfcl_grade,grade


def test_bfcl_types_and_optional_arguments():
    item={'function_name':'f','required':['n'],'answer':{'n':[1],'unit':['','m']}}
    assert bfcl_grade(item,'{"name":"f","arguments":{"n":1}}')==1
    assert bfcl_grade(item,'{"name":"f","arguments":{"n":true}}')==0
    assert bfcl_grade(item,'{"name":"f","arguments":{"n":2}}')==0
    assert bfcl_grade(item,'{"name":"f","arguments":{"n":1,"evil":2}}')==0


def test_empty_completed_responses_are_never_retried_on_resume(tmp_path):
    suite=[{'id':str(i),'source':'fixture','domain':d,'kind':'mcq','prompt':'A?','answer':'A'} for i,d in enumerate(['coding']*40+['co-work']*40+['reasoning']*40)]
    p=tmp_path/'suite.json';p.write_text(json.dumps(suite));key=tmp_path/'key';key.write_text('local-fixture-not-a-real-key')
    cfg={'suite_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'nltk_data':'','budget_usd':50,'max_output_tokens':512,'rootfs':'unused','discord_session':'fixture','provider_caps':{'mock':50},'models':[{'id':'m','label':'m','wire':'chat','key_file':str(key),'model':'fixture','endpoint':'https://example.invalid/v1/chat/completions','provider':'mock','price':{'input':1,'output':1}}]}
    from seb.ledger import Ledger
    prior=Ledger(tmp_path/'shared.sqlite');prior.wallet('random',50);prior.reserve('old','random','old',49);prior.finish('old',None,{},'unknown')
    cfg['ledger_path']=str(tmp_path/'shared.sqlite')
    (tmp_path/'run-config.json').write_text(json.dumps(cfg))
    class Response:
        status=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return json.dumps({'choices':[{'message':{'content':''},'finish_reason':'length'}],'usage':{'prompt_tokens':1,'completion_tokens':512,'total_tokens':513}}).encode()
    with patch('urllib.request.urlopen',return_value=Response()) as req,patch('subprocess.run'):
        main(tmp_path);assert req.call_count==120
        main(tmp_path);assert req.call_count==120
    state=json.loads((tmp_path/'state.json').read_text());assert state['phase']=='completed'
    assert state['accounting']['unknown_reserved_usd']==49
    assert not (tmp_path/'ledger.sqlite').exists()
    assert state['model_results'][0]['empty']==120
    assert state['model_results'][0]['scores']=={'coding':0.0,'co-work':0.0,'reasoning':0.0}


def test_broad_graders_rank_integrity_and_state_semantics():
    assert grade({'kind':'integer','answer':70},'Work\nAnswer: 070',None,None)['score']==1
    assert grade({'kind':'integer','answer':70},'170',None,None)['score']==0
    item={'kind':'ndcg','doc_ids':['a','b','c'],'qrels':{'a':3,'b':1,'elsewhere':2}}
    perfect=grade(item,'["a","b","c"]',None,None)['score']
    assert 0<perfect<1  # relevant document outside retrieval pool remains in IDCG
    assert grade(item,'["a","a","b"]',None,None)['score']==0
    assert grade(item,'["c","b","a"]',None,None)['score']<perfect
    state={'kind':'plan_state','answer':['holding_d','clear_a','on_a_b']}
    assert grade(state,'the hand is currently holding yellow block, the red block is clear and the red block is on top of the blue block',None,None)['score']==1
    assert grade(state,'the hand is empty',None,None)['score']==0


def test_shared_ledger_preserves_old_unknown_and_spending(tmp_path):
    from seb.ledger import Ledger,BudgetExceeded
    import pytest
    ledger=Ledger(tmp_path/'parent.sqlite');ledger.wallet('random',50)
    ledger.reserve('old','random','m',49);ledger.finish('old',None,{},'unknown')
    same=Ledger(tmp_path/'parent.sqlite');same.wallet('random',50)
    with pytest.raises(BudgetExceeded):same.reserve('new','random','m',2)
    assert same.status()[0]['outstanding']==49


def test_infobench_judge_accounted_once_and_replayed(tmp_path):
    from seb.random_judge import judge_infobench
    from seb.ledger import Ledger
    k=tmp_path/'key';k.write_text('fixture')
    cfg={'judge':{'model':'judge','price':{'input':1,'output':1},'key_file':str(k),'endpoint':'https://example.invalid'},'provider_caps':{'openai':40}}
    l=Ledger(tmp_path/'ledger');l.wallet('random',50)
    class Response:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self):return json.dumps({'output':[{'content':[{'type':'output_text','text':'[true, false]'}]}],'usage':{'input_tokens':10,'output_tokens':10}}).encode()
    item={'prompt':'Write two things.','criteria':['first?','second?']}
    with patch('urllib.request.urlopen',return_value=Response()) as req:
        assert judge_infobench(item,'one',cfg,l,tmp_path)['score']==0.5
        assert judge_infobench(item,'one',cfg,l,tmp_path)['score']==0.5
        assert req.call_count==1
    assert l.status()[0]['calls']==1 and l.status()[0]['charged']>0
