"""Resume-safe independent baseline measurement. No researcher or auto budget reset."""
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid
from .ledger import Ledger,BudgetExceeded
from .gateway import reservation,cost
from .random_baseline import grade


def save(path,obj):
    temp=path.with_name(path.name+'.tmp');temp.write_text(json.dumps(obj,ensure_ascii=False,indent=2));temp.replace(path)


def main(root):
    root=Path(root);cfg=json.loads((root/'run-config.json').read_text())
    assert hashlib.sha256((root/'suite.json').read_bytes()).hexdigest()==cfg['suite_sha256']
    lock=open(root/'run.lock','w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    sys.path.insert(0,cfg.get('vendor_path',str(root/'vendor')));os.environ['NLTK_DATA']=cfg['nltk_data']
    suite=json.loads((root/'suite.json').read_text())
    pilot=([next(x for x in suite if x['source']==source) for source in dict.fromkeys(x['source'] for x in suite)] if cfg.get('pilot_each_source') else [x for d in ['coding','co-work','reasoning'] for x in [a for a in suite if a['domain']==d][:2]])
    pilot_ids={x['id'] for x in pilot};suite=pilot+[x for x in suite if x['id'] not in pilot_ids]
    ledger=Ledger(cfg.get('ledger_path',root/'ledger.sqlite'));ledger.wallet('random',cfg['budget_usd'])
    started=time.time();models=cfg['models'];results=root/'results';results.mkdir(exist_ok=True)
    reusable={}
    for source in cfg.get('reuse_roots',[]):
        source=Path(source);oldcfg=json.loads((source/'run-config.json').read_text())
        for old in json.loads((source/'suite.json').read_text()):
            reusable[hashlib.sha256(json.dumps(old,sort_keys=True).encode()).hexdigest()]=(source,oldcfg,old)

    def measure(m):
        modelroot=results/m['id'];modelroot.mkdir(exist_ok=True)
        key=Path(m['key_file']).read_text().strip();done=[]
        for item in suite:
            path=modelroot/(hashlib.sha256(item['id'].encode()).hexdigest()[:20]+'.json')
            if path.exists():done.append(json.loads(path.read_text()));continue
            record={'item_id':item['id'],'source':item['source'],'domain':item['domain'],'model':m['id'],'attempts':[]}
            candidate=reusable.get(hashlib.sha256(json.dumps(item,sort_keys=True).encode()).hexdigest())
            if candidate:
                source,oldcfg,old=candidate
                matching=[x for x in oldcfg['models'] if x==m]
                oldpath=source/'results'/m['id']/path.name
                if matching and oldcfg['max_output_tokens']==cfg['max_output_tokens'] and oldpath.exists():
                    previous=json.loads(oldpath.read_text())
                    if previous.get('status')=='completed':
                        previous.update(reused_from=str(oldpath),new_provider_charge_usd=0)
                        save(path,previous);done.append(previous);continue

            if len(done)>=len(pilot) and any(x['status']!='completed' for x in done[:len(pilot)]):
                record.update(status='not_run_preflight_failure');save(path,record);done.append(record);continue
            # A persisted receipt predating a crash must not cause a duplicate provider call.
            reqroot=modelroot/path.stem;reqroot.mkdir(exist_ok=True)
            prior=list(reqroot.glob('attempt-*/request.json'))
            if prior:
                record.update(status='infra_unknown_previous_attempt');save(path,record);done.append(record);continue
            native=m['wire']=='responses'
            body={'model':m['model'],'input':item['prompt'],'max_output_tokens':cfg['max_output_tokens'],'reasoning':{'effort':m['effort']},'store':False} if native else {'model':m['model'],'messages':[{'role':'user','content':item['prompt']}],'max_tokens':cfg['max_output_tokens'],'stream':False}
            for attempt in range(3):
                cid=uuid.uuid4().hex;out=reqroot/f'attempt-{attempt:02d}';out.mkdir(exist_ok=True)
                amount=reservation({**body,'max_tokens':cfg['max_output_tokens']},m['price'])
                try:ledger.reserve(cid,'random',m['id'],amount,scopes={'provider:'+m['provider']:cfg['provider_caps'][m['provider']],'model:'+m['id']:30})
                except BudgetExceeded as e:
                    record.update(status='budget_exhausted',error=str(e));break
                save(out/'request.json',body);call={'call_id':cid,'started':time.time(),'reserve_usd':amount};record['attempts'].append(call)
                try:
                    req=urllib.request.Request(m['endpoint'],data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
                    with urllib.request.urlopen(req,timeout=600) as response:raw=response.read();call['http_status']=response.status
                    (out/'response.json').write_bytes(raw);d=json.loads(raw);usage=d.get('usage',{});charge=cost(usage,m['price'])
                    ledger.finish(cid,charge,usage,'completed' if charge is not None else 'unknown');call.update(charge_usd=charge,usage=usage,finished=time.time())
                    if native:
                        text=''.join(t.get('text','') for o in d.get('output',[]) for t in o.get('content',[]) if t.get('type')=='output_text');finish=d.get('status')
                    else:
                        choices=d.get('choices',[])
                        if not choices:raise ValueError('No choices')
                        text=choices[0].get('message',{}).get('content') or '';finish=choices[0].get('finish_reason')
                    record.update(status='completed',text=text,empty=not bool(text.strip()),finish_reason=finish,usage=usage,accounting_complete=charge is not None)
                    try:
                        if item['kind']=='infobench' and text.strip():
                            from .random_judge import judge_infobench
                            record.update(judge_infobench(item,text,cfg,ledger,reqroot))
                        else:record.update(grade(item,text,cfg['rootfs'],root))
                    except Exception as e:record.update(status='grader_error',error=type(e).__name__+': '+str(e))
                    save(out/'meta.json',call);break
                except urllib.error.HTTPError as e:
                    error=e.read().decode(errors='replace').replace(key,'[REDACTED]');(out/'error.txt').write_text(error);ledger.finish(cid,None,{},'unknown');call.update(http_status=e.code,error=error,finished=time.time());save(out/'meta.json',call)
                    record.update(status='infra_error',error=error)
                    if e.code not in (408,429,500,502,503,504):break
                    if attempt<2:time.sleep(10*(attempt+1))
                except (TimeoutError,urllib.error.URLError) as e:
                    ledger.finish(cid,None,{},'unknown');call.update(error=type(e).__name__,finished=time.time());save(out/'meta.json',call);record.update(status='infra_error',error=type(e).__name__)
                    if attempt<2:time.sleep(10*(attempt+1))
                except Exception as e:
                    # If a successful response was already accounted, preserve its original settlement.
                    with ledger.connect() as db:state=db.execute('SELECT state FROM calls WHERE id=?',(cid,)).fetchone()['state']
                    if state=='reserved':ledger.finish(cid,None,{},'unknown')
                    call.update(error=type(e).__name__,finished=time.time());save(out/'meta.json',call);record.update(status='infra_error',error=type(e).__name__);break
            save(path,record);done.append(record)
            summary={'model':m['id'],'finished_items':len(done),'completed':sum(x['status']=='completed' for x in done),'updated':time.time()};save(modelroot/'progress.json',summary)
        scores={}
        for domain in ['coding','co-work','reasoning']:
            subset=[x for x in done if x['domain']==domain]
            scores[domain]=sum(x['score'] for x in subset)/40 if len(subset)==40 and all(x['status']=='completed' for x in subset) else None
        result={'model':m['id'],'label':m['label'],'scores':scores,'completed':sum(x['status']=='completed' for x in done),'empty':sum(x.get('empty',False) for x in done),'counts':{s:sum(x['status']==s for x in done) for s in sorted({x['status'] for x in done})}}
        save(modelroot/'summary.json',result);return result
    save(root/'state.json',{'phase':'running','started':started,'job_id':os.environ.get('SLURM_JOB_ID'),'models':len(models),'items':len(suite)})
    outcomes=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures=[pool.submit(measure,m) for m in models]
        for future in concurrent.futures.as_completed(futures):
            try:outcomes.append(future.result())
            except Exception as e:outcomes.append({'error':type(e).__name__+': '+str(e)})
            save(root/'summary.json',outcomes)
    with ledger.connect() as db:
        calls=db.execute('SELECT COUNT(*),COALESCE(SUM(charged),0),COALESCE(SUM(CASE WHEN charged IS NULL THEN reserve ELSE 0 END),0) FROM calls').fetchone()
    state={'phase':'completed' if all(x.get('completed')==120 for x in outcomes) else 'incomplete','started':started,'finished':time.time(),'job_id':os.environ.get('SLURM_JOB_ID'),'model_results':outcomes,'accounting':{'calls':calls[0],'known_standard_usd':calls[1],'unknown_reserved_usd':calls[2]}}
    save(root/'state.json',state)
    # Transport-only terminal notification; no new agent or automatic researcher launch.
    import subprocess
    note=root/'terminal-message.txt';note.write_text('Random基线已结束：'+state['phase']+'，Slurm '+str(state['job_id'])+'。完整模型 '+str(sum(x.get('completed')==120 for x in outcomes))+'/8。产物 '+str(root)+'。请核对逐题结果、三领域相关与独立$50账本；不要重置预算或自动重测已完成错答/空答。')
    subprocess.run([sys.executable,'/nfs/stak/users/zhanyaol/.codex/skills/discord/scripts/discord_tool.py','send','--session',cfg['discord_session'],'--text-file',str(note)],timeout=30,check=False)

if __name__=='__main__':main(sys.argv[1])
