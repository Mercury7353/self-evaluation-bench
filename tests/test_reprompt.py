import json
from pathlib import Path
from unittest.mock import patch
import pytest
from seb import codex_harness as h
from seb.gateway import reservation,cost


def exercise(tmp_path, outcomes, policy=None, stop=None):
    clock=[0.];seen=[]
    def launch(root,workspace,trace,socket,token,model,prompt,**kw):
        elapsed,rc,message=outcomes[len(seen)]
        seen.append((prompt,kw));clock[0]+=elapsed
        trace=Path(trace);trace.mkdir(parents=True)
        events=[{'type':'turn.completed'}] if rc==0 else [{'type':'turn.failed','error':{'message':message}}]
        (trace/'codex.stdout').write_text('\n'.join(json.dumps(e) for e in events))
        (trace/'thread.json').write_text(json.dumps({'thread_id':'original-thread'}))
        (trace/'codex.process.json').write_text('{}')
        return rc
    with patch.object(h,'_launch_codex_once',launch),patch.object(h.time,'monotonic',lambda:clock[0]),patch.object(h.time,'sleep',lambda t:clock.__setitem__(0,clock[0]+t)):
        rc=h.launch_codex(tmp_path,tmp_path,tmp_path/'trace','socket','token','model','initial',timeout=10800,effort='max',stop_requested=stop,
            continuation_policy=policy or {'reprompt_remaining_seconds':5400,'transport_retries':2})
    return rc,seen,json.loads((tmp_path/'trace/continuations.json').read_text())


def test_early_return_resumes_same_session_and_original_deadline(tmp_path):
    rc,seen,log=exercise(tmp_path,[(120,0,''),(5500,0,'')])
    assert rc==0 and len(seen)==2
    assert seen[1][1]['resume_thread']=='original-thread'
    assert seen[1][1]['timeout']<10680
    assert log['attempts'][0]['decision']=='early_return'
    assert (tmp_path/'trace/attempt-001/codex.stdout').exists()


def test_transport_retry_preserves_failure_and_final_projection(tmp_path):
    rc,seen,log=exercise(tmp_path,[(120,1,'stream disconnected before completion'),(5500,0,'')])
    assert rc==0 and len(seen)==2
    assert log['attempts'][0]['decision']=='transport_recovery'
    assert 'turn.failed' in (tmp_path/'trace/attempt-001/codex.stdout').read_text()
    assert 'turn.failed' not in (tmp_path/'trace/codex.stdout').read_text()


@pytest.mark.parametrize('message',['policy denied','quota exceeded','unrelated error','stream disconnected: 429 rate limit'])
def test_no_retry_for_nontransport_or_blocks(tmp_path,message):
    rc,seen,log=exercise(tmp_path,[(10,1,message)])
    assert rc==1 and len(seen)==1


def test_retry_bound(tmp_path):
    rc,seen,log=exercise(tmp_path,[(10,1,'network error')]*3)
    assert rc==1 and len(seen)==3


def test_stop_guard_prevents_reprompt(tmp_path):
    rc,seen,log=exercise(tmp_path,[(10,0,'')],stop=lambda:'researcher_budget_limit')
    assert len(seen)==1 and log['attempts'][0]['stop_reason']=='researcher_budget_limit'


def test_reservation_floor_does_not_change_request_or_charge():
    body={'max_tokens':1024,'messages':[]};price={'input':1,'output':5}
    usage={'prompt_tokens':130,'completion_tokens':11746,'total_tokens':11876}
    guarded=dict(price,reservation_output_floor=131072)
    assert reservation(body,guarded)>cost(usage,guarded)>reservation(body,price)
    assert body['max_tokens']==1024 and cost(usage,price)==cost(usage,guarded)


def test_closed_development_wallet_stops_research(tmp_path):
    from test_research_lifecycle import lifecycle
    from seb.ledger import Ledger
    life,work,out=lifecycle(tmp_path)
    ledger=Ledger(out/'gateway/ledger.sqlite')
    ledger.wallet('designer',100);ledger.wallet('development',0)
    assert life.poll()=='development_accounting_guard'
    assert life.evidence['wallet']=='development'
