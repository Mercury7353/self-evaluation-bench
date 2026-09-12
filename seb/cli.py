"""Portable entry points for evaluating an executable suite."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from .container import contained, image_root
from .evaluation import load_manifest
from .execution_policy import DEFAULT_POLICY
from .ledger import Ledger
from .research import validate_submission
from .runner import digest_tree


def write(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp')
    temp.write_text(json.dumps(value,indent=2)+'\n');temp.replace(path)


def request(socket, token, route, data=None):
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket)),base_url='http://localhost',timeout=30) as client:
        r=client.request('GET' if data is None else 'POST',route,headers={'x-api-key':token},json=data)
        if r.is_error:raise RuntimeError(f'Gateway HTTP {r.status_code}: {r.text[:500]}')
        return r.json()


def doctor(root):
    root=Path(root).resolve()
    if not shutil.which('bwrap'):raise RuntimeError('Install bubblewrap (bwrap) on this Linux host')
    if not (root/'usr/local/bin/python').is_file():raise ValueError('Rootfs must provide /usr/local/bin/python')
    # This is the same user/network namespace entry path used by actual suites.
    result=subprocess.run(contained(root,['/usr/local/bin/python','-c','import sys;print(sys.version.split()[0])']),capture_output=True,text=True,timeout=30)
    if result.returncode:raise RuntimeError('Namespace/rootfs check failed: '+result.stderr[-1000:])
    return {'python_in_rootfs':result.stdout.strip(),'bwrap':shutil.which('bwrap'),'paid_api_calls':0}


def mock_provider():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path.endswith('/count_tokens'):
                raw=b'{"input_tokens":12}';self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
            prompt=json.dumps(body.get('messages',[]))
            answer='6' if '2 * 3' in prompt else '4'
            if body['model'].endswith('-0') or (body['model'].endswith('-1') and '2 * 3' in prompt):answer='0'
            value={'id':'mock-response','type':'message','role':'assistant','model':body['model'],
                   'content':[{'type':'text','text':answer}], 'stop_reason':'end_turn',
                   'usage':{'input_tokens':12,'output_tokens':1}}
            if body.get('stream'):
                events=[{'type':'message_start','message':dict(value,content=[],stop_reason=None)},
                    {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
                    {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':answer}},
                    {'type':'content_block_stop','index':0},
                    {'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':value['usage']},
                    {'type':'message_stop'}]
                raw=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
            raw=json.dumps(value).encode();self.send_response(200)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server


def make_config(settings, rootfs, output, socket, *, upstream=None):
    aliases=[m['id'] for m in settings['models']]
    if not aliases or len(aliases)!=len(set(aliases)):raise ValueError('Model IDs must be nonempty and unique')
    cap=settings['budget_usd']
    if isinstance(cap,bool) or not isinstance(cap,(int,float)) or not 0<cap<float('inf'):raise ValueError('budget_usd must be finite and positive')
    key='local-mock-only' if upstream else os.environ.get(settings['key_env'],'')
    if not key:raise ValueError('Set the provider key environment variable named by key_env')
    secret=output/'provider.secret';secret.write_text(key);secret.chmod(0o600)
    token=secrets.token_hex(24)
    cfg={'artifacts':str(output/'gateway'),'run_root':str(output),'key_file':str(secret),'upstream':upstream or settings['upstream'],
         'base_root':str(rootfs),'gateway_socket':str(socket),'image_cache':str(output/'image-cache'),
         'prices':{m['id']:m['price'] for m in settings['models']},
         'model_backends':{m['id']:{'model':m['model']} for m in settings['models']},
         'efforts':{m['id']:m['effort'] for m in settings['models'] if m.get('effort')},
         'evaluation_policy':DEFAULT_POLICY | settings.get('evaluation_policy',{}),
         'minimum_items':settings.get('minimum_items',100),'require_item_budgets':True,
         'suite_cost_cap_usd':settings.get('suite_cost_cap_usd',cap),
         'item_cost_cap_usd':settings.get('item_cost_cap_usd',cap),
         'suite_concurrency':settings.get('model_concurrency',2),
         'request_concurrency_per_model':settings.get('request_concurrency_per_model',2),
         'suite_timeout':settings.get('timeout_seconds',7200),'reservation_wait_seconds':30,
         'tokens':{token:{'role':'evaluation','wallet':'evaluation','cap':cap,'models':aliases,
                          'workspace':str(output/'submissions'),'allow_suite':True}}}
    return cfg,token


def evaluate(args):
    source=Path(args.submission).resolve();rootfs=Path(args.rootfs).resolve();out=Path(args.output).resolve()
    validate_submission(source)
    settings=({'budget_usd':1,'minimum_items':2,'suite_cost_cap_usd':1,'item_cost_cap_usd':.5,
               'timeout_seconds':60,'models':[{'id':'mock-model','model':'mock-provider','price':{'input':1,'output':1}}]}
              if args.mock else json.loads(Path(args.config).read_text()))
    load_manifest(source,settings.get('minimum_items',100))
    doctor(rootfs)  # No model call before local runtime validation.
    out.mkdir(parents=True,exist_ok=False);out.chmod(0o700)
    server=None;process=None;result=None
    state={'phase':'preparing','jobs':{},'started':time.time(),'mock':args.mock,'models':[m['id'] for m in settings['models']]}
    def update(**kw):state.update(kw);write(out/'state.json',state)
    def interrupted(signum,frame):raise InterruptedError('Evaluation interrupted; inspect this run before starting another')
    prior=signal.signal(signal.SIGTERM,interrupted)
    try:
        if args.mock:server=mock_provider()
        with tempfile.TemporaryDirectory(prefix='seb-socket-') as socketdir:
            socket=Path(socketdir)/'gateway.sock'
            cfg,token=make_config(settings,rootfs,out,socket,upstream=f'http://127.0.0.1:{server.server_port}' if server else None)
            # Validate price/output policy and preserve a frozen input snapshot.
            from .execution_policy import policy_for
            policy_for(cfg)
            from .gateway import reservation
            for model in settings['models']:
                reservation({'max_tokens':cfg['evaluation_policy']['default_output_tokens'],'messages':[]},model['price'])
            shutil.copytree(source,out/'submissions/suite');write(out/'submission.sha256.json',digest_tree(out/'submissions/suite'))
            private=out/'gateway.private.json';write(private,cfg);private.chmod(0o600)
            write(out/'settings.json',settings)
            with (out/'gateway.stdout').open('wb') as stdout,(out/'gateway.stderr').open('wb') as stderr:
                process=subprocess.Popen([sys.executable,'-m','seb.research','--config',str(private),'--uds',str(socket)],stdout=stdout,stderr=stderr)
            for _ in range(200):
                try:
                    if request(socket,token,'/health').get('status')=='ok':break
                except (httpx.HTTPError,OSError):pass
                if process.poll() is not None:raise RuntimeError('Gateway startup failed; inspect gateway.stderr')
                time.sleep(.1)
            else:raise TimeoutError('Gateway startup timed out')
            deadline=time.time()+cfg['suite_timeout']
            for model in settings['models']:
                job=request(socket,token,'/research/suites',{'path':'suite','model':model['id']})
                state['jobs'][model['id']]=job['id'];update(phase='evaluating')
            results={}
            while len(results)<len(state['jobs']) and time.time()<deadline:
                for model,jid in state['jobs'].items():
                    if model in results:continue
                    row=request(socket,token,'/research/jobs/'+jid)
                    if row['status'] not in ('queued','running'):
                        results[model]={'model':model,'job_id':jid,**row}
                        write(out/'results.json',list(results.values()))
                update(finished_jobs=len(results),total_jobs=len(state['jobs']))
                if len(results)<len(state['jobs']):time.sleep(1)
            for model,jid in state['jobs'].items():
                if model not in results:results[model]={'model':model,'job_id':jid,'status':'incomplete','score_status':'incomplete','reason':'controller_deadline'}
            rows=list(results.values());write(out/'results.json',rows)
            complete=all(r.get('score_status')=='valid' for r in rows)
            result={'phase':'completed' if complete else 'incomplete','models':len(rows),
                    'valid_models':sum(r.get('score_status')=='valid' for r in rows),'output':str(out),
                    'scores':[{'model':r['model'],'score':r.get('result',{}).get('score'),'score_status':r.get('score_status')} for r in rows],
                    'wallets':Ledger(out/'gateway/ledger.sqlite').status(),**({'paid_api_calls':0} if args.mock else {})}
            update(**result,finished=time.time());print(json.dumps(result,indent=2))
    except BaseException as e:
        update(phase='failed',error=type(e).__name__+': '+str(e),finished=time.time());raise
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=30)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        if server:server.shutdown();server.server_close()
        # Gateway credentials are transient; response traces and accounting remain private.
        (out/'provider.secret').unlink(missing_ok=True)
        (out/'gateway.private.json').unlink(missing_ok=True)
        signal.signal(signal.SIGTERM,prior)
    return 0 if result and result['phase']=='completed' else 2


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    d=sub.add_parser('doctor');d.add_argument('--rootfs',required=True)
    i=sub.add_parser('init-rootfs');i.add_argument('--image',default='python:3.12-slim');i.add_argument('--cache',required=True)
    v=sub.add_parser('validate');v.add_argument('submission');v.add_argument('--minimum-items',type=int,default=100)
    b=sub.add_parser('budget');b.add_argument('run')
    e=sub.add_parser('evaluate');e.add_argument('submission');e.add_argument('--config');e.add_argument('--mock',action='store_true');e.add_argument('--rootfs',required=True);e.add_argument('--output',required=True)
    x=sub.add_parser('experiment');xs=x.add_subparsers(dest='experiment_command',required=True)
    xv=xs.add_parser('validate');xv.add_argument('--config',required=True)
    xr=xs.add_parser('run');xr.add_argument('--config',required=True);xr.add_argument('--researcher');xr.add_argument('--output',required=True);xr.add_argument('--mock',action='store_true')
    a=p.parse_args()
    if a.command=='experiment':
        from .experiment_config import load,describe
        if a.experiment_command=='validate':print(json.dumps(describe(load(a.config)),indent=2))
        else:
            from .experiment import run
            result=run(a.config,a.researcher,a.output,mock=a.mock);print(json.dumps(result,indent=2))
            raise SystemExit(0 if result['eligible'] else 2)
    elif a.command=='doctor':print(json.dumps(doctor(a.rootfs),indent=2))
    elif a.command=='init-rootfs':print(image_root(a.image,a.cache))
    elif a.command=='validate':
        validate_submission(Path(a.submission));m=load_manifest(Path(a.submission),a.minimum_items);print(json.dumps({'valid':True,'items':len(m['items'])}))
    elif a.command=='budget':
        path=Path(a.run)/'gateway/ledger.sqlite'
        if not path.is_file():p.error('No ledger found in this run')
        print(json.dumps(Ledger(path).status(),indent=2))
    else:
        if not a.mock and not a.config:p.error('evaluate requires --config or --mock')
        if a.mock and a.config:p.error('Use either --mock or --config')
        raise SystemExit(evaluate(a))


if __name__=='__main__':main()
