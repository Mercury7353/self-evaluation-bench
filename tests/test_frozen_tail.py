import json
import pytest
from seb.frozen_tail import prepare,merge


def test_preserves_wrong_and_empty_and_only_selects_missing(tmp_path):
 s=tmp_path/'source';s.mkdir()
 (s/'run.py').write_text('FROZEN=True\n')
 (s/'questions.json').write_text(json.dumps({'direct_items':[{'id':'a'},{'id':'b'},{'id':'c'}],'scenarios':[]}))
 manifest={'items':[{'id':k} for k in 'abc']};(s/'evaluation.json').write_text(json.dumps(manifest))
 old={'items':[{'id':'a','execution_status':'completed','answer_status':'missing','score':0},{'id':'b','execution_status':'completed','answer_status':'answered','score':0},{'id':'c','execution_status':'not_run','evidence':[]}]}
 plan=prepare(s,tmp_path/'tail',old,'m')
 assert plan['missing']==['c']
 assert (tmp_path/'tail/frozen_program.py').read_bytes()==(s/'run.py').read_bytes()
 result=merge(old,{'items':[{'id':'c','execution_status':'completed','score':1}]},manifest)
 assert result['items'][:2]==old['items'][:2]
 with pytest.raises(ValueError):merge(old,{'items':[{'id':'a','score':1}]},manifest)


def test_unknown_call_cannot_be_resampled(tmp_path):
 s=tmp_path/'source';s.mkdir()
 (s/'evaluation.json').write_text('{"items":[{"id":"a"}]}')
 (s/'questions.json').write_text('{"direct_items":[{"id":"a"}],"scenarios":[]}')
 with pytest.raises(ValueError,match='Unreconciled'):
  prepare(s,tmp_path/'tail',{'items':[{'id':'a','execution_status':'infra_error','evidence':[{'id':'old'}]}]},'m')
