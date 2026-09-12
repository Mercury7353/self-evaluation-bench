"""Research workspace services and a format-neutral executable evaluation contract."""
import asyncio
import hashlib
import ipaddress
import json
import math
import os
import shutil
import socket
import time
import traceback
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .gateway import create_app
from .container import contained,run_logged,safe_path
from .runner import digest_tree,execute_task
from .ledger import Ledger
from .execution_policy import policy_for, output_limit
from .evaluation import load_manifest, normalize_result, attach_accounting, public_contract


def write_json(path, value):
    path=Path(path);tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    tmp.write_text(json.dumps(value,indent=2));tmp.replace(path)


def scoped_cost(config,scope):
    """Exact trial attribution, including nested agents, without wallet deltas."""
    root=Path(config['artifacts']);ledger=Ledger(root/'ledger.sqlite')
    summary={'charged_usd':0.,'outstanding_reserved_usd':0.,'calls':0,
             'rejected_requests':0,'cost_complete':True,'usage_by_model':{},'pending_jobs':[]}
    with ledger.connect() as db:
        for marker in (root/'scopes'/scope).glob('*'):
            if not marker.is_file():continue
            row=db.execute('SELECT * FROM calls WHERE id=?',(marker.name,)).fetchone()
            if row is None:
                path=root/'wire'/marker.name/'meta.json'
                meta=json.loads(path.read_text()) if path.exists() else {}
                if meta.get('state')=='rejected_budget':summary['rejected_requests']+=1
                else:summary['cost_complete']=False
                continue
            summary['calls']+=1
            if row['charged'] is None:
                summary['outstanding_reserved_usd']+=row['reserve'];summary['cost_complete']=False
            else:summary['charged_usd']+=row['charged']
            usage=json.loads(row['usage']) if row['usage'] else {}
            per_model=summary['usage_by_model'].setdefault(row['model'],{})
            for key,value in usage.items():
                if isinstance(value,(int,float)) and not isinstance(value,bool):per_model[key]=per_model.get(key,0)+value
    for marker in (root/'scopes'/scope/'jobs').glob('*'):
        result_path=root.parent/'research-jobs'/marker.name/'result.json'
        try:status=json.loads(result_path.read_text()).get('status')
        except (FileNotFoundError,ValueError):status=None
        if status in (None,'queued','running'):
            summary['pending_jobs'].append(marker.name);summary['cost_complete']=False
    return summary


def validate_submission(path):
    path=Path(path)
    if not (path/'run.py').is_file():raise ValueError('Submission requires run.py')
    if not (path/'README.md').is_file():raise ValueError('Submission requires README.md with run instructions')
    hashes=digest_tree(path)
    return hashes


def finite(value):
    return isinstance(value,(float,int)) and not isinstance(value,bool) and math.isfinite(value)


def validate_result(result,ledger,wallet,started,jobs_root):
    if not finite(result.get('score')):raise ValueError('score must be finite')
    items=result.get('items')
    if not isinstance(items,list) or not items:raise ValueError('items must be a nonempty list')
    ids=set()
    with ledger.connect() as db:
        for item in items:
            name=item.get('id')
            if not isinstance(name,str) or not name or name in ids:raise ValueError('Item IDs must be unique nonempty strings')
            ids.add(name)
            if not finite(item.get('score')):raise ValueError('Item score must be finite')
            evidence=item.get('evidence')
            if not isinstance(evidence,list) or not evidence:raise ValueError('Every item requires call/trial evidence')
            for e in evidence:
                eid=e.get('id','')
                if len(eid)!=32 or any(c not in '0123456789abcdef' for c in eid):raise ValueError('Invalid evidence ID')
                if e.get('kind')=='llm':
                    row=db.execute('SELECT wallet,created,charged,state FROM calls WHERE id=?',(eid,)).fetchone()
                    if not row or row['wallet']!=wallet or row['created']<started or row['charged'] is None or row['state']!='completed':
                        raise ValueError('Evidence must be a completed call in this execution and wallet')
                elif e.get('kind')=='agent':
                    r=json.loads((Path(jobs_root)/eid/'request.json').read_text())
                    if r['wallet']!=wallet or r['created']<started:raise ValueError('Agent evidence outside this execution')
                    a=json.loads((Path(jobs_root)/eid/'result.json').read_text())
                    if a['status']!='ok':raise ValueError('Agent evidence did not complete successfully')
                else:raise ValueError('Unknown evidence kind')
    for k,v in result.get('capabilities',{}).items():
        if not isinstance(k,str) or not finite(v):raise ValueError('Capability scores must be finite')
    return result


def run_suite(source,model,token,config,output,entry):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    started=time.time();state={'status':'running','model':model,'started':started}
    result_path=output/'result.json';result_path.write_text(json.dumps(state))
    scope=output.parent.name
    if len(scope)!=32 or any(c not in '0123456789abcdef' for c in scope):scope=uuid.uuid4().hex
    state['trace_scope']=scope
    child_token=None
    try:
        before=validate_submission(source)
        protocol=policy_for(config,entry)
        manifest=load_manifest(source,entry.get('minimum_items',config.get('minimum_items',100))) if protocol else None
        work=output/'workspace';shutil.copytree(source,work)
        if digest_tree(work)!=before:raise ValueError('Submission changed while snapshotting')
        (output/'submission.sha256.json').write_text(json.dumps(before,indent=2))
        root=output/'rootfs';shutil.copytree(config['base_root'],root,symlinks=True)
        # The entry's workspace is resolved by the server, never sent by a client.
        child_token=uuid.uuid4().hex
        child_entry={k:v for k,v in entry.items() if k not in ('workspace','research','_active_workspace')}
        child_entry.update(workspace=str(work),research=False,allow_suite=False,models=list(dict.fromkeys([model]+config.get('auxiliary_models',[]))),
                           trace_scopes=list(dict.fromkeys(entry.get('trace_scopes',[])+[scope])))
        if config.get('require_item_budgets'):
            child_entry.update(budget_scopes={**entry.get('budget_scopes',{}),
                               'suite:'+scope:entry.get('suite_cost_cap_usd',config['suite_cost_cap_usd'])},
                               item_scope_prefix='item:'+scope+':',allowed_item_ids=list({i.get('budget_group',i['id']) for i in manifest['items']}))
        config['tokens'][child_token]=child_entry
        context={'model':model,'token':child_token,'base_url':'http://127.0.0.1:18765',
                 'output_dir':'/workspace/raw','efforts':config.get('efforts',{}),'budget_usd':entry['cap']}
        if protocol:
            context.update(public_contract(config,entry),execution_id=scope,seed=config.get('evaluation_seed',0),
                           submission_sha256=before,efforts={model:config.get('efforts',{}).get(model)})
        ctx=output/'context.private.json';ctx.write_text(json.dumps(context));ctx.chmod(0o600)
        command=['/usr/local/bin/python','/workspace/run.py','--context','/run/context.json','--output','/workspace/result.json']
        launch=output/'launch.private.json';launch.write_text(json.dumps({'command':command,'cwd':'/workspace','env':{'SEB_CONTEXT':'/run/context.json','PYTHONPATH':'/opt:/opt/science'}}));launch.chmod(0o600)
        package=Path(__file__).parent
        binds=[(work,'/workspace',False),(ctx,'/run/context.json',True),(launch,'/run/launch.json',True),
               (config['gateway_socket'],'/run/gateway.sock',False),
               (package/'sandbox_entry.py','/opt/entry.py',True),(package/'research_sdk.py','/opt/research_sdk.py',True)]
        if config.get('science_packages'):
            binds.append((config['science_packages'],'/opt/science',True))
        rc=run_logged(contained(root,['/usr/local/bin/python','/opt/entry.py'],cwd='/workspace',binds=binds),output/'program',timeout=config.get('suite_timeout',1800))
        state['returncode']=rc
        if rc:raise RuntimeError('Submitted program failed; see program stdout/stderr')
        result=json.loads((work/'result.json').read_text())
        ledger=Ledger(Path(config['artifacts'])/'ledger.sqlite')
        if protocol:
            result=normalize_result(result,manifest,ledger,entry['wallet'],started,Path(config['artifacts']).parent/'research-jobs')
            if config.get('require_item_budgets'):
                from .evidence import check_budget_evidence
                check_budget_evidence(result,manifest,ledger,scope,Path(config['artifacts']).parent/'research-jobs')
        else:
            validate_result(result,ledger,entry['wallet'],started,Path(config['artifacts']).parent/'research-jobs')
        wallet=ledger.status(entry['wallet'])[0]
        own_cost=scoped_cost(config,scope)
        if not protocol and (not own_cost['cost_complete'] or wallet['charged']>entry['cap']+1e-9):
            raise ValueError('Execution has pending jobs, unknown costs, or exceeded the wallet budget')
        state.update(status='ok',result=result,wallet=wallet)
        if protocol:
            state.update(score_status=result['score_status'],execution_status=result['execution_status'])
            if result['score_status']!='valid':state['status']='incomplete'
            if own_cost['pending_jobs']:
                state.update(status='incomplete',execution_status='incomplete',pending_jobs=own_cost['pending_jobs'])
    except Exception as e:
        state.update(status='error',error=type(e).__name__+': '+str(e))
        (output/'exception.txt').write_text(traceback.format_exc())
    finally:
        if child_token:config['tokens'].pop(child_token,None)
        for name in ['launch.private.json','context.private.json']:(output/name).unlink(missing_ok=True)
    attach_accounting(state,scoped_cost(config,scope))
    state['finished']=time.time();write_json(result_path,state);return state


def research_app(config):
    app=create_app(config)
    root=Path(config['artifacts']).parent
    jobs=root/'research-jobs';jobs.mkdir(exist_ok=True)
    locks={};live=set();jobs_by_id={}
    def access(request):
        token=request.headers.get('x-api-key') or request.headers.get('authorization','').removeprefix('Bearer ')
        return token,config['tokens'].get(token)
    def workspace(entry):
        return entry.get('_active_workspace') or entry.get('workspace') or config.get('workspace')

    @app.get('/research/info')
    async def info(request:Request):
        token,entry=access(request)
        if not entry:return JSONResponse({'error':'Unauthorized'},401)
        return {'models':entry['models'],'contract':public_contract(config,entry),
                'efforts':{m:config.get('efforts',{}).get(m) for m in entry['models']}}

    @app.post('/research/feedback')
    async def whitebox_feedback(request:Request):
        _,entry=access(request)
        if not entry or not entry.get('research'):return JSONResponse({'error':'Development access required'},403)
        try:
            from .feedback import feedback
            data=await request.json()
            # Serialize numeric feedback: immutable cache paths must never race.
            async with locks.setdefault(('feedback',entry['wallet']),asyncio.Lock()):
                return await asyncio.to_thread(feedback,config,entry,jobs,data.get('job_ids'))
        except Exception as e:return JSONResponse({'error':str(e)},400)

    async def submit(request,kind):
        token,entry=access(request)
        if not entry:return JSONResponse({'error':'Unauthorized'},401)
        if kind=='suite' and not (entry.get('research') or entry.get('allow_suite')):return JSONResponse({'error':'Suite execution access required'},403)
        data=await request.json()
        if kind=='suite' and entry.get('start_deadline_on_first_suite') and 'deadline_epoch' not in entry:
            entry['deadline_epoch']=time.time()+entry['start_deadline_on_first_suite']
        try:
            if time.time()>=entry.get('deadline_epoch',float('inf')):raise ValueError('The wallet execution deadline has passed')
            agent_scopes=dict(entry.get('budget_scopes',{}))
            if kind=='agent' and config.get('require_item_budgets'):
                item_id=request.headers.get('x-seb-item-id')
                if not entry.get('item_scope_prefix') or item_id not in entry.get('allowed_item_ids',[]):
                    raise ValueError('Agent tasks require a declared item ID inside a budgeted suite or pilot')
                agent_scopes[entry['item_scope_prefix']+hashlib.sha256(item_id.encode()).hexdigest()]=config['item_cost_cap_usd']
            pilot=data.get('pilot',False)
            if type(pilot) is not bool:raise ValueError('pilot must be boolean')
            if pilot and (kind!='suite' or not entry.get('research') or not config.get('allow_pilots')):
                raise ValueError('Small pilots are available only to the development researcher')
            if entry.get('research') and time.time()>=config.get('research_deadline_epoch',float('inf')):
                raise ValueError('The research submission deadline has passed')
            maximum=config.get('max_pending_suites') if kind=='suite' else config.get('max_pending_agents')
            if maximum:
                pending=sum(1 for ident in jobs_by_id
                    if (meta:=json.loads((jobs/ident/'request.json').read_text()))['wallet']==entry['wallet'] and meta['kind']==kind)
                if pending>=maximum:
                    return JSONResponse({'error':'Queue full; inspect or cancel existing queued jobs before submitting',
                                         'pending':pending,'limit':maximum},409)
            output_tokens=output_limit(config,data.get('max_output_tokens'),entry)
            if 'max_output_tokens' in data and kind!='agent':raise ValueError('max_output_tokens applies to agent tasks only')
            model=data['model']
            if model not in entry['models']:raise ValueError('Model not allowed')
            rel=str(data['path'])
            if rel.startswith('/workspace/'):rel=rel[len('/workspace/'):]
            elif rel.startswith('/'):raise ValueError('Use a workspace-relative path')
            path=safe_path(workspace(entry),rel)
            if kind=='suite':
                validate_submission(path)
                if policy_for(config,entry):load_manifest(path,1 if pilot else config.get('minimum_items',100))
            else:
                from harbor.models.task.task import Task
                digest_tree(path);Task(path)
        except Exception as e:return JSONResponse({'error':str(e)},400)
        ident=uuid.uuid4().hex;out=jobs/ident;out.mkdir()
        # Snapshot at submission, not when it eventually leaves the queue.
        snapshot=out/'submitted';shutil.copytree(path,snapshot)
        submission_id=hashlib.sha256(json.dumps(digest_tree(snapshot),sort_keys=True).encode()).hexdigest()
        (out/'request.json').write_text(json.dumps({'kind':kind,'model':model,'wallet':entry['wallet'],'created':time.time(),'path':str(path),
                                                  'submission_id':submission_id,'pilot':pilot,
                                                  **({'budget_scopes':agent_scopes} if kind=='agent' and config.get('require_item_budgets') else {}),
                                                  'candidate_output_tokens':output_tokens if kind=='agent' else None}))
        write_json(out/'result.json',{'id':ident,'status':'queued','queued_at':time.time(),'submission_id':submission_id})
        for ancestor in entry.get('trace_scopes',[]):
            index=Path(config['artifacts'])/'scopes'/ancestor/'jobs';index.mkdir(parents=True,exist_ok=True)
            (index/ident).touch()
        async def background():
            try:
                limit=config.get('suite_concurrency',1) if kind=='suite' else config.get('agent_concurrency',1)
                lock=locks.setdefault((entry['wallet'],kind),asyncio.Semaphore(limit))
                async with lock:
                    write_json(out/'result.json',{'id':ident,'status':'running','started':time.time(),'submission_id':submission_id})
                    if kind=='suite':result=await asyncio.to_thread(run_suite,snapshot,model,token,config,out/'execution',
                        dict(entry,minimum_items=1) if pilot else entry)
                    else:
                        agent_token=uuid.uuid4().hex
                        config['tokens'][agent_token]=dict(entry,research=False,allow_suite=False,models=[model],
                            trace_scopes=list(dict.fromkeys(entry.get('trace_scopes',[])+[ident])))
                        if config.get('require_item_budgets'):
                            config['tokens'][agent_token]['budget_scopes']=agent_scopes
                            config['tokens'][agent_token].pop('item_scope_prefix',None)
                        try:
                            result=await asyncio.to_thread(execute_task,snapshot,model,agent_token,
                                                          dict(config,candidate_output_tokens=output_tokens),out/'execution')
                        finally:config['tokens'].pop(agent_token,None)
                        result.update(trace_scope=ident,cost=scoped_cost(config,ident))
                        (out/'execution/result.json').write_text(json.dumps(result,indent=2))
                    result.update(id=ident,submission_id=submission_id,pilot=pilot)
                    write_json(out/'result.json',result)
            except Exception as e:write_json(out/'result.json',{'id':ident,'status':'error','error':str(e),'submission_id':submission_id})
        job=asyncio.create_task(background());live.add(job);job.add_done_callback(live.discard)
        jobs_by_id[ident]=job
        job.add_done_callback(lambda _:jobs_by_id.pop(ident,None))
        return {'id':ident,'status':'queued','submission_id':submission_id,'pilot':pilot}

    @app.get('/research/jobs')
    async def job_list(request:Request):
        _,entry=access(request)
        if not entry:return JSONResponse({'error':'Unauthorized'},401)
        rows=[]
        for folder in jobs.iterdir():
            try:
                meta=json.loads((folder/'request.json').read_text())
                if meta['wallet']!=entry['wallet'] or meta.get('model') not in entry['models']:continue
                status=json.loads((folder/'result.json').read_text())
            except (FileNotFoundError,ValueError):continue
            rows.append({'id':folder.name,'model':meta['model'],'kind':meta['kind'],
                'submission_id':meta.get('submission_id'),'pilot':meta.get('pilot',False),
                'created':meta['created'],'status':status['status'],'started':status.get('started'),
                'finished':status.get('finished'),'score_status':status.get('score_status'),
                'score':status.get('result',{}).get('score'),'cost':status.get('cost')})
        rows.sort(key=lambda r:r['created'])
        return {'jobs':rows,'checked_at':time.time(),'suite_concurrency':config.get('suite_concurrency',1),
                'max_pending_suites':config.get('max_pending_suites')}

    @app.post('/research/suites')
    async def suite(request:Request):return await submit(request,'suite')

    @app.post('/research/agents')
    async def agent(request:Request):return await submit(request,'agent')

    @app.post('/research/jobs/{ident}/cancel')
    async def cancel(request:Request,ident:str):
        _,entry=access(request)
        if not entry:return JSONResponse({'error':'Unauthorized'},401)
        if len(ident)!=32 or any(c not in '0123456789abcdef' for c in ident):return JSONResponse({'error':'Invalid ID'},400)
        try:
            folder=jobs/ident
            meta=json.loads((folder/'request.json').read_text())
            if meta['wallet']!=entry['wallet'] or meta.get('model') not in entry['models']:
                return JSONResponse({'error':'Forbidden'},403)
            previous=json.loads((folder/'result.json').read_text())
            if previous['status']!='queued' or ident not in jobs_by_id:
                return JSONResponse({'error':'Only queued jobs can be cancelled'},409)
            jobs_by_id[ident].cancel()
            state={'id':ident,'status':'cancelled','finished':time.time()}
            (folder/'result.json').write_text(json.dumps(state));return state
        except FileNotFoundError:return JSONResponse({'error':'Unknown job'},404)

    @app.get('/research/jobs/{ident}')
    async def job(request:Request,ident:str):
        token,entry=access(request)
        if not entry:return JSONResponse({'error':'Unauthorized'},401)
        if len(ident)!=32 or any(c not in '0123456789abcdef' for c in ident):return JSONResponse({'error':'Invalid ID'},400)
        folder=jobs/ident
        try:
            meta=json.loads((folder/'request.json').read_text())
            if meta['wallet']!=entry['wallet'] or meta.get('model') not in entry['models']:return JSONResponse({'error':'Forbidden'},403)
            return json.loads((folder/'result.json').read_text())
        except FileNotFoundError:return JSONResponse({'error':'Unknown job'},404)

    @app.get('/research/jobs/{ident}/artifacts')
    @app.get('/research/jobs/{ident}/artifacts/{relative:path}')
    async def artifacts(request:Request,ident:str,relative:str=''):
        _,entry=access(request)
        if not entry:return JSONResponse({'error':'Unauthorized'},401)
        if len(ident)!=32 or any(c not in '0123456789abcdef' for c in ident):return JSONResponse({'error':'Invalid ID'},400)
        folder=jobs/ident
        try:
            meta=json.loads((folder/'request.json').read_text())
            if meta['wallet']!=entry['wallet'] or meta.get('model') not in entry['models']:return JSONResponse({'error':'Forbidden'},403)
            base=folder/'execution'
            def allowed(path):
                parts=path.relative_to(base).parts
                return (not any(p in ('rootfs','root','workspace','task') for p in parts)
                        and not any('.private' in p or p=='access.json' for p in parts))
            if not relative:
                names=[]
                for directory,dirs,files in os.walk(base):
                    dirs[:]=[d for d in dirs if allowed(Path(directory)/d)]
                    names.extend(str((Path(directory)/name).relative_to(base)) for name in files
                                 if allowed(Path(directory)/name))
                return {'files':sorted(names)}
            path=safe_path(base,relative)
            if not allowed(path):return JSONResponse({'error':'Private or mutable workspace artifact'},403)
            return Response(path.read_bytes(),media_type='application/octet-stream')
        except FileNotFoundError:return JSONResponse({'error':'Not found'},404)
        except ValueError as e:return JSONResponse({'error':str(e)},400)
    return app


def main():
    import argparse,uvicorn
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--uds',required=True);a=p.parse_args()
    config=json.loads(Path(a.config).read_text());uvicorn.run(research_app(config),uds=a.uds,access_log=False)

if __name__=='__main__':main()
