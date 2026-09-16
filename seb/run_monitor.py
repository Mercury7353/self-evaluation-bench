"""Transport-only experiment monitor; no model, agent or new experiment is launched."""
import argparse
import json
import subprocess
import shutil
import sys
import time
from pathlib import Path


def concise_notice(info, plan):
    phases={'preparing':'准备中','preflight':'接口预检','designing':'研究中','acceptance':'最终验收中',
            'completed':'控制器已结束','failed':'控制器失败','incomplete':'验收未完整',
            'pending_reference':'参考成绩未齐','pending_provider':'接口结果未齐'}
    phase=info.get('phase','unknown')
    lines=[f"新 Sol 自动监控：{phases.get(phase,phase)}；调度 {info.get('scheduler','unknown')}。"]
    if phase=='designing':
        lines.append('研究仍在进行，尚未形成最终全量验收结果。')
    if info.get('alert')=='stalled':
        lines.append(f"连续 {info.get('stalled_seconds',0)} 秒无新落盘记录，需要检查；不代表进程已停止。")
    if info.get('model_guards'):
        lines.append('有模型触发计费保护，需核对账本后处理。')
    if info.get('alert')=='terminal':
        lines.append('请核验逐题覆盖与终态，作业结束不等于全量测完。')
    if plan.get('report_url'):lines.append('报告：'+plan['report_url'])
    return '\n'.join(lines)


def snapshot(root):
    root=Path(root);state={};p=root/'state.json'
    if p.exists():state=json.loads(p.read_text())
    jobs={}
    paths=[p for pattern in ['research-jobs/*/request.json','round-*/research-jobs/*/request.json','mimo-correction/research-jobs/*/request.json','runs/*/research-jobs/*/request.json'] for p in root.glob(pattern)]
    for p in sorted(paths, key=lambda p:p.stat().st_mtime):
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
    for p in [p for pattern in ['gateway/model-guards/*','round-*/gateway/model-guards/*','mimo-correction/gateway/model-guards/*'] for p in root.glob(pattern)]:
        try:guards.append(json.loads(p.read_text()).get('model'))
        except ValueError:pass
    traces={str(p.relative_to(root)):p.stat().st_size for p in root.glob('researcher-trace-*/attempt-*/codex.stdout')}
    return {'trace_bytes':traces,'checked':time.time(),'phase':state.get('phase','not_started'),'jobs':jobs,'model_guards':guards,'coverage':state.get('coverage',{}),'encumbered':state.get('encumbered_by_recovery_model',{})}


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--session',required=True)
    p.add_argument('--discord-tool',required=True);a=p.parse_args();root=Path(a.root)
    plan=json.loads((root/'plan.json').read_text());deadline=plan['deadline_epoch'];last_send=0;last_progress=time.time();previous=None;last_alert=None
    while True:
        try:
            info=snapshot(root);progress=json.dumps([info['jobs'],info['trace_bytes']],sort_keys=True)
            if progress!=previous:last_progress=time.time();previous=progress
            info['stalled_seconds']=round(time.time()-last_progress)
            jobfile=root/'submission.json'
            if jobfile.exists():
                job=json.loads(jobfile.read_text())['stdout'].strip().split(';')[0]
                result=subprocess.run([shutil.which('sacct') or '/apps/slurm/current/bin/sacct','-j',job,'--noheader','--format=JobIDRaw,State','-P'],capture_output=True,text=True,timeout=20)
                info['scheduler']=next((line.split('|')[1] for line in result.stdout.splitlines() if line.split('|')[0]==job),'unknown')
            terminal=info['phase'] in ('completed','failed','blocked','incomplete','pending_reference','pending_provider') or info.get('scheduler') in ('FAILED','CANCELLED','TIMEOUT','OUT_OF_MEMORY')
            if info['phase'] not in ('completed','failed','blocked','incomplete','pending_reference','pending_provider') and info.get('scheduler') not in ('COMPLETED','FAILED','CANCELLED','TIMEOUT','OUT_OF_MEMORY') and any(j['state'] in ('running','queued') for j in info['jobs'].values()) and time.time()<deadline:terminal=False
            alert= ('terminal' if terminal else 'model_guard' if info['model_guards'] else 'stalled' if info['stalled_seconds']>=300 else 'progress')
            info['alert']=alert
            (root/'monitor-heartbeat.json').write_text(json.dumps(info,indent=2))
            if time.time()-last_send>=plan.get('notification_interval_seconds',300) or alert!=last_alert and alert!='progress' or terminal:
                message=concise_notice(info,plan) if plan.get('concise_notifications') else 'Sol研究与全模型验收自动监控：'+json.dumps(info,ensure_ascii=False)+'\n报告：'+plan.get('report_url','https://github.com/Mercury7353/MyContext/blob/research/self-evaluation-benchmark/visualizations/sol-reprompt-3h-20260915.html')
                note=root/'monitor-message.txt';note.write_text(message)
                sent=subprocess.run([sys.executable,a.discord_tool,'send','--session',a.session,'--text-file',str(note)],capture_output=True,text=True,timeout=40)
                with (root/'monitor-delivery.jsonl').open('a') as f:f.write(json.dumps({'at':time.time(),'returncode':sent.returncode,'output':sent.stdout,'error':sent.stderr})+'\n')
                last_send=time.time();last_alert=alert
            if terminal or time.time()>deadline+180:break
        except Exception as e:
            with (root/'monitor-errors.jsonl').open('a') as f:f.write(json.dumps({'at':time.time(),'error':type(e).__name__+': '+str(e)})+'\n')
        time.sleep(60)

if __name__=='__main__':main()
