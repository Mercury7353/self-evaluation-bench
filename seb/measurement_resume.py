"""Controller-owned reuse of responses from the exact frozen program and model.

No caller-supplied score or cross-version evidence is trusted. Historical charges
and unknown reservations stay in their original wallet; reused calls cost zero anew.
"""
import hashlib
import json
from pathlib import Path
from .runner import digest_tree
from .evaluation import check_evidence, finite
from .ledger import Ledger


def fingerprint(source):
    return hashlib.sha256(json.dumps(digest_tree(source),sort_keys=True).encode()).hexdigest()


def replay_key(body, group, sample=None):
    return hashlib.sha256(json.dumps({'body':body,'group':group,'sample':sample},sort_keys=True,separators=(',',':')).encode()).hexdigest()


def collect(config, source, model, current_job):
    root=Path(config['run_root']);gateway=Path(config['artifacts']);jobs=root/'research-jobs'
    ledger=Ledger(gateway/'ledger.sqlite');fp=fingerprint(source)
    manifest=json.loads((Path(source)/'evaluation.json').read_text());items={i['id']:i for i in manifest['items']}
    groups={i:irow.get('budget_group',i) for i,irow in items.items()};group_items={g:[i for i,v in groups.items() if v==g] for g in set(groups.values())}
    completed={};replies={};agents={};agent_results={};allowed={};sources=[];calls=set();active=[]
    for p in sorted(jobs.glob('*/request.json'),key=lambda p:p.stat().st_mtime):
        if p.parent.name==current_job:continue
        meta=json.loads(p.read_text())
        if meta.get('kind')!='suite' or meta.get('model')!=model or meta.get('submission_id')!=fp:continue
        # Verify immutable source rather than relying only on its recorded hash.
        if fingerprint(p.parent/'submitted')!=fp:raise ValueError('Prior frozen submission changed')
        state_path=p.parent/'result.json'
        state=json.loads(state_path.read_text()) if state_path.exists() else {}
        if state.get('status')=='running':active.append(p.parent.name);continue
        if state.get('status')=='queued':continue
        scope=p.parent.name;sources.append(scope)
        with ledger.connect() as db:
            rows=db.execute('SELECT DISTINCT c.* FROM calls c JOIN call_budget_scopes s ON c.id=s.call_id WHERE s.scope=?',('suite:'+scope,)).fetchall()
            for row in rows:
                calls.add(row['id'])
                if row['state']!='completed':continue
                membership={v['scope'] for v in db.execute('SELECT scope FROM call_budget_scopes WHERE call_id=?',(row['id'],))}
                call_groups=[g for g in group_items if 'item:'+scope+':'+hashlib.sha256(g.encode()).hexdigest() in membership]
                if not call_groups:continue
                allowed[row['id']]={'items':sorted({i for g in call_groups for i in group_items[g]}),'source_job':scope,'wallet':row['wallet']}
                wire=gateway/'wire'/row['id'];mp=wire/'meta.json'
                if not mp.exists():continue
                wiremeta=json.loads(mp.read_text());operation=wiremeta.get('operation_id')
                if not operation:continue
                op=gateway/'operations'/hashlib.sha256((row['wallet']+'\0'+operation).encode()).hexdigest()
                if not (op/'result.json').exists():continue
                response_meta=json.loads((op/'result.json').read_text())
                if response_meta.get('http_status')!=200 or response_meta.get('headers',{}).get('x-seb-request-id')!=row['id']:continue
                try:
                    data=json.loads((op/'response.body').read_text());body=json.loads((wire/'request.body').read_text())
                except (ValueError,FileNotFoundError):continue
                if not isinstance(data.get('content'),list):continue
                # Explicit sample IDs are appended to the root suite sample path.
                sample_path=wiremeta.get('cache_sample_path',[]);base=meta.get('cache_sample_path',[])
                if sample_path[:len(base)]!=base or len(sample_path)>len(base)+1:continue
                sample=sample_path[-1] if len(sample_path)>len(base) else None
                reply={'text':''.join(c.get('text','') for c in data['content'] if c.get('type')=='text'),
                       'response':data,'evidence':{'kind':'llm','id':row['id']},'operation_id':operation,
                       'attempt_count':len(response_meta.get('attempts',[])),'finish_reason':data.get('stop_reason'),'resumed_from':scope}
                for g in call_groups:replies.setdefault(replay_key(body,g,sample),reply)
        for child in (gateway/'scopes'/scope/'jobs').glob('*'):
            child_folder=jobs/child.name
            try:
                child_meta=json.loads((child_folder/'request.json').read_text());child_result=json.loads((child_folder/'result.json').read_text())
            except (ValueError,FileNotFoundError):continue
            if child_meta.get('kind')!='agent' or child_result.get('status')!='ok':continue
            membership=child_meta.get('budget_scopes',{})
            matched=[g for g in group_items if 'item:'+scope+':'+hashlib.sha256(g.encode()).hexdigest() in membership]
            if matched:allowed[child.name]={'items':sorted({i for g in matched for i in group_items[g]}),'source_job':scope,'wallet':child_meta['wallet'],'kind':'agent'}
            request_body=child_meta.get('client_request')
            if matched and request_body:
                for g in matched:agents.setdefault(replay_key({'agent':request_body},g),{'id':child.name,'status':'ok'})
                agent_results[child.name]=child_result

        raw=state.get('result')
        if not raw:
            partial=p.parent/'execution/workspace/result.json'
            if partial.exists():
                try:raw=json.loads(partial.read_text())
                except ValueError:raw=None
        for item in (raw or {}).get('items',[]):
            ident=item.get('id')
            if ident not in items or ident in completed or item.get('execution_status')!='completed':continue
            if not finite(item.get('score')) or not 0<=item['score']<=1:continue
            if item.get('answer_status') not in ('answered','missing','invalid','refused'):continue
            if item.get('answer_status')!='answered' and item['score']!=0:continue
            try:
                check_evidence(item,ledger,meta['wallet'],meta['created'],jobs,completed=True,candidate_model=model,prior_evidence=allowed)
                if any(e.get('kind') not in ('llm','agent') or ident not in allowed.get(e['id'],{}).get('items',[]) for e in item.get('evidence',[])):continue
            except (ValueError,KeyError):continue
            completed[ident]=item
    with ledger.connect() as db:
        cost=sum((row['charged'] if row['charged'] is not None else row['reserve']) for ident in calls for row in db.execute('SELECT charged,reserve FROM calls WHERE id=?',(ident,)))
    return {'version':1,'submission_id':fp,'model':model,'completed_items':completed,'replies':replies,
            'agents':agents,'agent_results':agent_results,'prior_evidence':allowed,'source_jobs':sources,'active_jobs':active,'prior_call_ids':sorted(calls),
            'prior_encumbered_usd':cost,'item_budget_groups':groups}
