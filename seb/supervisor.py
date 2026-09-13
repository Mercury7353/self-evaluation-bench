"""Gateway lifecycle, immutable submissions and bounded model panels."""
from concurrent.futures import ThreadPoolExecutor,as_completed
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from .cli import write,request
from .research import validate_submission
from .runner import digest_tree

def run_jobs(config,token,models,path,output,deadline,*,pilot=False):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    rows=[]
    def trial(model):
        marker=output/(model+'.job.json')
        if marker.exists():job=json.loads(marker.read_text())
        else:
            if time.time()>=deadline:raise TimeoutError('No new submission after deadline')
            job=request(config['gateway_socket'],token,'/research/suites',{'path':path,'model':model,'pilot':pilot})
            write(marker,job)
        while time.time()<deadline:
            result=request(config['gateway_socket'],token,'/research/jobs/'+job['id'])
            if result['status'] not in ('queued','running'):
                return {'model':model,'job_id':job['id'],**result}
            time.sleep(2)
        return {'model':model,'job_id':job['id'],'status':'incomplete','score_status':'incomplete','reason':'deadline'}
    with ThreadPoolExecutor(max_workers=config.get('suite_concurrency',4)) as pool:
        pending={pool.submit(trial,m):m for m in models}
        for future in as_completed(pending):
            try:row=future.result()
            except Exception as e:row={'model':pending[future],'status':'error','score_status':'incomplete','error':str(e)}
            rows.append(row);write(output/'results.json',rows)
    return rows


def start_gateway(config,root):
    config_path=Path(root)/'gateway.private.json';write(config_path,config);config_path.chmod(0o600)
    with (Path(root)/'gateway.stdout').open('ab') as out,(Path(root)/'gateway.stderr').open('ab') as err:
        process=subprocess.Popen([sys.executable,'-m','seb.research','--config',str(config_path),'--uds',config['gateway_socket']],
                                 stdout=out,stderr=err,start_new_session=True)
    for _ in range(200):
        try:
            if request(config['gateway_socket'],'unused','/health').get('status')=='ok':return process
        except Exception:pass
        if process.poll() is not None:raise RuntimeError('Gateway failed to start')
        time.sleep(.1)
    process.terminate();raise TimeoutError('Gateway startup timeout')


def stop_gateway(process):
    if process and process.poll() is None:
        process.terminate()
        try:process.wait(timeout=30)
        except subprocess.TimeoutExpired:process.kill();process.wait()

def check_designer_exit(trace, returncode, *, harness='claude_code'):
    if harness=='codex':
        path=Path(trace)/'codex.stdout'
        events=[]
        if path.exists():
            for line in path.read_text().splitlines():
                try:events.append(json.loads(line))
                except ValueError:pass
        failed=any(e.get('type') in ('turn.failed','error') for e in events)
        completed=any(e.get('type')=='turn.completed' for e in events)
        if failed or returncode not in (0,124) or returncode==0 and not completed:
            raise RuntimeError(f'Codex researcher exited {returncode}; inspect the native SDK trace')
        return
    result = {}
    path = Path(trace)/'claude.stdout'
    if path.exists():
        for line in path.read_text().splitlines():
            try: event = json.loads(line)
            except ValueError: continue
            if event.get('type') == 'result': result = event
    if returncode not in (0, 124) or result.get('is_error'):
        raise RuntimeError(f'Designer exited {returncode}: ' + str(result.get('result', 'see designer trace'))[:2000])


def freeze_program(source, destination):
    hashes=validate_submission(source)
    shutil.copytree(source,destination)
    if digest_tree(destination)!=hashes:raise ValueError('Submission changed during freezing')
    destination.with_suffix('.sha256.json').write_text(json.dumps(hashes,indent=2))
