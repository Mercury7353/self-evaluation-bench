"""Same-session Claude research continuation with the original clock and wallet."""
import json
import shutil
import time
import uuid
from pathlib import Path


def terminal(trace):
    session=None;result=None
    for line in (Path(trace)/'claude.stdout').read_text().splitlines():
        try:event=json.loads(line)
        except ValueError:continue
        if event.get('session_id'):session=str(uuid.UUID(event['session_id']))
        if event.get('type')=='result':result=event
    return session,result


def transport_error(result):
    # Only terminal error metadata, never researcher prose or tool outputs.
    if not result or not result.get('is_error'):return False
    text=json.dumps({k:result.get(k) for k in ('errors','error','result')}).lower()
    if any(s in text for s in ('policy','quota','unauthorized','authentication','budget','rate limit','403','401','429')):return False
    return any(s in text for s in ('stream disconnected','connection reset','connection closed','error decoding response body','network error'))


def launch(once,root,workspace,trace,gateway_socket,token,model,prompt,*,timeout,continuation_policy,resume_session=None,stop_requested=None,**kwargs):
    trace=Path(trace);trace.mkdir(parents=True,exist_ok=True)
    deadline=time.monotonic()+timeout;records=[];failures=0;current=prompt
    while True:
        attempt=trace/f'attempt-{len(records)+1:03d}'
        rc=once(root,workspace,attempt,gateway_socket,token,model,current,
            timeout=max(.001,deadline-time.monotonic()),resume_session=resume_session,stop_requested=stop_requested,**kwargs)
        for name in ('claude.stdout','claude.stderr','claude.process.json'):
            if (attempt/name).exists():shutil.copyfile(attempt/name,trace/name)
        (trace/'prompt.txt').write_text(prompt)
        session,result=terminal(attempt)
        if resume_session and session and session!=resume_session:raise ValueError('Claude continuation changed session identity')
        resume_session=session or resume_session
        remaining=deadline-time.monotonic();stopped=stop_requested() if stop_requested else None
        reason='finished';delay=0
        if not stopped and resume_session and remaining>0:
            if rc==0 and result and not result.get('is_error') and remaining>=continuation_policy['reprompt_remaining_seconds']:
                reason='early_return';delay=1
            elif transport_error(result) and failures<continuation_policy['transport_retries']:
                failures+=1;reason='transport_recovery';delay=min(30,2**failures)
        records.append(dict(attempt=str(attempt),returncode=rc,session_id=resume_session,remaining_seconds=remaining,decision=reason,stop_reason=stopped,at=time.time()))
        (trace/'continuations.json').write_text(json.dumps({'policy':continuation_policy,'projection':'Parent mirrors final attempt; immutable history in attempt directories.','attempts':records},indent=2))
        if reason=='finished':return rc if result else (rc or 1)
        time.sleep(min(delay,max(0,remaining)))
        if time.monotonic()>=deadline:return rc
        current=(f'You still have {int(deadline-time.monotonic())} seconds remaining in the original research window. '
            'Please continue improving your benchmark and maximize its evaluation quality. '
            'Continue this same session and workspace with the original wallets and deadline. '
            'Inspect existing test job IDs and saved responses; do not repeat completed wrong or empty answers. '
            'Choose your own research methods and testing schedule. '
            +('The preceding turn was interrupted by a transport failure; reconcile pending operations before issuing new work.' if reason=='transport_recovery' else ''))
