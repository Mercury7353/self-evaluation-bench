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
    assert state['model_results'][0]['empty']==120
    assert state['model_results'][0]['scores']=={'coding':0.0,'co-work':0.0,'reasoning':0.0}
