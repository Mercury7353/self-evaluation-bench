import json
from pathlib import Path
from unittest.mock import patch
import pytest
from seb import claude_continuation as h

SID='11111111-1111-4111-8111-111111111111'
def exercise(tmp_path,outcomes,stop=None):
    clock=[0.];seen=[]
    def once(root,workspace,trace,socket,token,model,prompt,**kw):
        elapsed,error=outcomes[len(seen)];seen.append((prompt,kw));clock[0]+=elapsed
        trace=Path(trace);trace.mkdir(parents=True)
        (trace/'claude.stdout').write_text(json.dumps({'type':'result','session_id':SID,'is_error':bool(error),'result':error or 'done'}))
        return int(bool(error))
    with patch.object(h.time,'monotonic',lambda:clock[0]),patch.object(h.time,'sleep',lambda n:clock.__setitem__(0,clock[0]+n)):
        rc=h.launch(once,tmp_path,tmp_path,tmp_path/'trace','socket','token','glm','initial',timeout=10800,continuation_policy={'reprompt_remaining_seconds':5400,'transport_retries':2},stop_requested=stop)
    return rc,seen,json.loads((tmp_path/'trace/continuations.json').read_text())

def test_same_session_and_clock(tmp_path):
    rc,seen,log=exercise(tmp_path,[(120,None),(5500,None)])
    assert rc==0 and len(seen)==2 and seen[1][1]['resume_session']==SID
    assert seen[1][1]['timeout']<10680
    assert log['attempts'][0]['decision']=='early_return'
    assert (tmp_path/'trace/attempt-001/claude.stdout').exists()

@pytest.mark.parametrize('err',['policy denied','quota exceeded','network error 429','unrelated error'])
def test_nontransport_does_not_retry(tmp_path,err):
    rc,seen,_=exercise(tmp_path,[(10,err)]);assert rc==1 and len(seen)==1

def test_transport_bound(tmp_path):
    rc,seen,_=exercise(tmp_path,[(10,'network error')]*3);assert rc==1 and len(seen)==3

def test_stop_guard(tmp_path):
    _,seen,log=exercise(tmp_path,[(10,None)],lambda:'researcher_budget_limit')
    assert len(seen)==1 and log['attempts'][0]['stop_reason']=='researcher_budget_limit'

def test_model_prose_cannot_trigger_transport_retry():
    assert not h.transport_error({'is_error':False,'result':'network error'})
