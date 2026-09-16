"""Transport-only experiment monitor; no model, agent or new experiment is launched."""
import argparse
import json
import subprocess
import shutil
import sys
import time
from pathlib import Path


def snapshot(root):
    root=Path(root);state={};p=root/'state.json'
    if p.exists():state=json.loads(p.read_text())
    jobs={}
    for p in sorted(root.glob('**/research-jobs/*/request.json'), key=lambda p:p.stat().st_mtime):
        request=json.loads(p.read_text());result_path=p.parent/'result.json'
        if not result_path.exists():continue
        result=json.loads(result_path.read_text())
        rows=(result.get('result') or {}).get('items',[])
        partial=p.parent/'execution/workspace/result.json'
        if not rows and partial.exists():
            try:rows=json.loads(partial.read_text()).get('items',[])
            except ValueError:pass
        jobs[request['model']]={'state':result.get('status'),'recorded':len(rows),
            'completed':sum(r.get('execution_status')=='completed' for r in rows)}
    guards=[]
    for p in root.glob('**/gateway/model-guards/*'):
        try:guards.append(json.loads(p.read_text()).get('model'))
        except ValueError:pass
    return {'checked':time.time(),'phase':state.get('phase','not_started'),'jobs':jobs,'model_guards':guards,'coverage':state.get('coverage',{}),'encumbered':state.get('encumbered_by_recovery_model',{})}


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--session',required=True)
    p.add_argument('--discord-tool',required=True);a=p.parse_args();root=Path(a.root)
    deadline=json.loads((root/'plan.json').read_text())['deadline_epoch'];last_send=0;last_progress=time.time();previous=None;last_alert=None
    while True:
        try:
            info=snapshot(root);progress=json.dumps(info['jobs'],sort_keys=True)
            if progress!=previous:last_progress=time.time();previous=progress
            info['stalled_seconds']=round(time.time()-last_progress)
            jobfile=root/'submission.json'
            if jobfile.exists():
                job=json.loads(jobfile.read_text())['stdout'].strip().split(';')[0]
                result=subprocess.run([shutil.which('sacct') or '/apps/slurm/current/bin/sacct','-j',job,'--noheader','--format=JobIDRaw,State','-P'],capture_output=True,text=True,timeout=20)
                info['scheduler']=next((line.split('|')[1] for line in result.stdout.splitlines() if line.split('|')[0]==job),'unknown')
            terminal=info['phase'] in ('completed','failed','blocked') or info.get('scheduler') in ('FAILED','CANCELLED','TIMEOUT','OUT_OF_MEMORY')
            if info['phase'] not in ('completed','failed','blocked') and info.get('scheduler') not in ('COMPLETED','FAILED','CANCELLED','TIMEOUT','OUT_OF_MEMORY') and any(j['state'] in ('running','queued') for j in info['jobs'].values()) and time.time()<deadline:terminal=False
            alert= ('terminal' if terminal else 'model_guard' if info['model_guards'] else 'stalled' if info['stalled_seconds']>=300 else 'progress')
            info['alert']=alert
            (root/'monitor-heartbeat.json').write_text(json.dumps(info,indent=2))
            if time.time()-last_send>=300 or alert!=last_alert and alert!='progress' or terminal:
                note=root/'monitor-message.txt';note.write_text('Sol冻结补测自动监控：'+json.dumps(info,ensure_ascii=False)+'\n报告：https://github.com/Mercury7353/MyContext/blob/research/self-evaluation-benchmark/visualizations/sol-reprompt-3h-20260915.html')
                sent=subprocess.run([sys.executable,a.discord_tool,'send','--session',a.session,'--text-file',str(note)],capture_output=True,text=True,timeout=40)
                with (root/'monitor-delivery.jsonl').open('a') as f:f.write(json.dumps({'at':time.time(),'returncode':sent.returncode,'output':sent.stdout,'error':sent.stderr})+'\n')
                last_send=time.time();last_alert=alert
            if terminal or time.time()>deadline+180:break
        except Exception as e:
            with (root/'monitor-errors.jsonl').open('a') as f:f.write(json.dumps({'at':time.time(),'error':type(e).__name__+': '+str(e)})+'\n')
        time.sleep(60)

if __name__=='__main__':main()
