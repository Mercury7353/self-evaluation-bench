"""Run one YAML-selected researcher through development, freezing and acceptance."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import tempfile
import time

from .billing import cache_adjusted_cost
from .cli import doctor, mock_provider, request, write
from .container import launch_claude
from .evaluation import load_manifest, public_contract
from .experiment_config import load, describe, researcher_view
from .gateway import reservation
from .ledger import Ledger
from .runner import digest_tree
from .scoring import score_panel
from .domain_scoring import score_domain
from .supervisor import start_gateway, stop_gateway, run_jobs, freeze_program, check_designer_exit


TASK = '''You are a researcher building an inexpensive executable benchmark to measure model capabilities.
Use the visible targets, references and resources in whitebox.json and resources/. You may create tasks,
select existing tasks, or combine approaches. Choose your own research method within the provided budget.
Build submission/run.py, submission/evaluation.json and submission/README.md. The neutral executable
contract and SDK examples are in CONTRACT.md and research_sdk.py. Optional predictor.py may learn a
mapping from item measurements to target scores; its code is frozen before independent evaluation.
Only development models are accessible now. Future model families and additional targets are withheld.
Explore and test within the development wallet. Use the same job ID when polling; do not rerun an answer
because it was wrong, empty, or a refusal. Improve the actual benchmark between development iterations.
Submit executable code with all assets. The controller snapshots it after this round, returns white-box
feedback if another round is configured, and independently evaluates the final frozen submission.
'''

CONTRACT = '''# Researcher interface

The environment has Python 3.12, the SDK, and the mounted scientific packages. Internet is disabled.
Only explicitly supplied resources and scoped gateway operations are available. Candidate models and
visible target labels are in whitebox.json. `Client().info()` gives the runtime contract and budget limits.

```python
from research_sdk import Client
c = Client()
job = c.submit("submission", model="candidate-id", pilot=True)
result = c.wait(job["id"], timeout=3600)  # On timeout keep polling this ID.
feedback = c.feedback([job_id_a, job_id_b, job_id_c])
```

Small development pilots are allowed; final submissions must meet the configured minimum item count.
Feedback needs one completed job per model, at least three models, all from the same suite snapshot.
Scores, billing and execution completeness are distinct. Follow the per-item budget contract.

`run.py --context PATH --output PATH` receives a scoped evaluation context for ONE candidate model.
Use `Client(context).item(item_id, prompt, grader)`; grader(text) returns a number in [0,1]. The SDK
records evidence and distinguishes completed empty answers (zero) from unresolved infrastructure.
Write JSON `{"protocol_version":1,"items":[...]}` with exactly one result for EACH declared item.
Persist intermediate results atomically. Do not catch program bugs and turn them into wrong answers.
`Client.agent(task_path, item_id=...)` also supports a single-container Harbor task through Claude Code;
it requires the optional agent dependencies and an available task image.

`evaluation.json` example (extend to the required item count):
```json
{"protocol_version":1,"items":[{"id":"example"}],"selection":"fixed","aggregation":{"kind":"weighted_mean"}}
```
Items may carry positive weights and a shared budget_group. Adaptive selection or custom aggregation
must declare its frozen method in the manifest. Item IDs, graders, weights and assets are frozen together.
README.md explains execution. Keep transient outputs and caches outside submission/.

Optional `predictor.py` contract:
- fit(training_rows, target_metadata) -> fitted object
- predict(fitted, observations) -> {target_id: finite_score or {"status":"unsupported"}}
- training_rows: [{"observations": {item_id: score_or_null}, "targets": {target_id: score}}]
- observations: {item_id: score_or_null}; no candidate identity or test labels
- target_metadata: {target_id: {"scale": positive_number, "status": "primary_archival"}}
- scores at scale 1 must be in [0,1]. Other scales are in the reference's original units.
- optional read-only assets go in predictor_assets/. Each fold runs in a fresh offline process.

The platform reports raw-score correlations and family-disjoint predictor CV. If predictor.py is absent,
a fixed mean-feature ridge baseline is used and identified as such. Development feedback can guide
selection; hidden acceptance results are returned only to the operator after freezing and never to you.
'''


DOMAIN_TASK = """You are researching an inexpensive evaluation for the assigned capability domain.
Use the two visible target definitions and development resources in whitebox.json/resources.
Choose your own method: original-task selection, cheap-item selection, synthesis, rewriting,
mixed or adaptive measurement. You have one continuous research run; iteration is your choice.
Use the SDK to test on development candidates and inspect evidence within the stated budget.
Submit an executable measurement with a visible-target predictor and a fixed aggregate domain score.
The domain score (higher means better) is evaluated against additional undisclosed targets with
NO target-specific fitting. Independent acceptance uses held-out candidates, including new model
families and other configurations of known families. Do not look up candidate identities/scores.
Measure the final submission on development candidates before your deadline so its visible
predictor can use the final-snapshot development observations. Freeze all code and assets.
"""

DOMAIN_CONTRACT = """
# Domain protocol v1

One independent research run; do not wait for controller-managed research rounds.
The final held-out panel is separate from development. `predictor.py` is REQUIRED:
its frozen fit/predict code receives ONLY visible-target development labels, plus anonymous
item measurements. It never receives sealed target metadata/labels or held-out target labels.
Its fitted mapping may be precomputed in predictor_assets/ during research.
The frozen evaluation.json aggregation supplies z_domain; exactly that same score is used
for every sealed target in this domain. There is no label-based sign change or hidden refit.
Final acceptance returns separate visible and sealed utility. Missing outputs, constant
predictions, reference coverage, costs and infrastructure status are recorded distinctly.
"""


def metadata(cfg, visibility):
    return {t['id']:{'scale':t['scale'],'status':'primary_archival'} for t in cfg['benchmarks'][visibility]}


def build_gateway(cfg, researcher, out, socket, *, mock_url=None):
    used={researcher['provider']} | {m['provider'] for m in cfg['models']}
    secretfiles={}
    for pid in sorted(used):
        provider=cfg['providers'][pid]
        key='local-mock-only' if mock_url else os.environ.get(provider['key_env'],'')
        if not key:raise ValueError('Missing provider credential environment variable: '+provider['key_env'])
        secret=out/(pid+'.secret');secret.write_text(key);secret.chmod(0o600);secretfiles[pid]=str(secret)
    all_models=[researcher,*cfg['models']]
    tokens={name:secrets.token_hex(32) for name in ['designer','development','evaluation']}
    development=[m['id'] for m in cfg['models'] if m['split']=='development']
    first=cfg['providers'][researcher['provider']]
    budget=cfg['budgets']
    config={'artifacts':str(out/'gateway'),'run_root':str(out),'gateway_socket':str(socket),
        'base_root':cfg['runtime']['rootfs'],'science_packages':cfg['runtime']['science_packages'],
        'image_cache':str(out/'image-cache'),'key_file':secretfiles[researcher['provider']],
        'upstream':mock_url or first['upstream'],
        'prices':{m['id']:m['price'] for m in all_models},
        'model_backends':{m['id']:{'model':m['model'],'key_file':secretfiles[m['provider']],
                            'upstream':mock_url or cfg['providers'][m['provider']]['upstream']} for m in all_models},
        'efforts':{m['id']:m['effort'] for m in all_models if m.get('effort')},
        'evaluation_policy':cfg['evaluation']['policy'],'minimum_items':cfg['design']['minimum_items'],
        'require_item_budgets':True,'allow_pilots':True,'suite_cost_cap_usd':budget['suite_usd'],
        'item_cost_cap_usd':budget['item_usd'],'suite_cost_goal_usd':budget['suite_usd'],
        'suite_timeout':cfg['evaluation']['seconds'],'suite_concurrency':cfg['evaluation']['model_concurrency'],
        'request_concurrency_per_model':cfg['evaluation']['requests_per_model'],
        'max_pending_suites':max(len(cfg['models'])*2,8),'reservation_wait_seconds':15,
        'research_deadline_epoch':time.time()+cfg['design']['seconds'],
        'overall':cfg['overall'],
        'whitebox':{'models':[m for m in cfg['models'] if m['split']=='development'],
            'references':{t:row['scores'] for t,row in researcher_view(cfg)['targets'].items()},
            'targets':metadata(cfg,'whitebox')},
        'tokens':{tokens['designer']:{'wallet':'designer','cap':budget['researcher_usd'],'models':[researcher['id']],'deadline_epoch':time.time()+cfg['design']['seconds']},
            tokens['development']:{'wallet':'development','cap':budget['development_usd'],'models':development,
                'workspace':str(out/'researcher-work'),'research':True,'deadline_epoch':time.time()+cfg['design']['seconds']},
            tokens['evaluation']:{'wallet':'evaluation','cap':budget['evaluation_usd'],
                'models':[m['id'] for m in cfg['models'] if not cfg.get('domain_protocol') or m['split']=='holdout'],'workspace':str(out/'acceptance-input'),'allow_suite':True,'start_deadline_on_first_suite':cfg['evaluation']['seconds']}}}
    if budget.get('judge_usd'):
        config['tokens'][secrets.token_hex(32)]={'wallet':'judge','cap':budget['judge_usd'],'models':[]}
    if cfg['evaluation']['preflight']:
        tokens['preflight']=secrets.token_hex(32)
        config['tokens'][tokens['preflight']]={'wallet':'development','cap':budget['development_usd'],
            'models':[m['id'] for m in cfg['models']],'research':True,'workspace':str(out/'readiness'),
            'deadline_epoch':config['research_deadline_epoch']}
    for model in all_models:reservation({'max_tokens':cfg['evaluation']['policy']['default_output_tokens'],'messages':[]},model['price'])
    return config,tokens


def prepare_workspace(cfg, config, tokens, out):
    work=out/'researcher-work';work.mkdir()
    write(work/'whitebox.json',researcher_view(cfg))
    contract=CONTRACT
    if cfg.get('domain_protocol'):
        contract=CONTRACT.split('The platform reports raw-score correlations')[0]
        contract=contract.replace('Optional `predictor.py` contract:', 'Required `predictor.py` contract:')+DOMAIN_CONTRACT
    (work/'CONTRACT.md').write_text(contract)
    shutil.copy2(Path(__file__).with_name('research_sdk.py'),work/'research_sdk.py')
    for target in cfg['benchmarks']['whitebox']:
        for index,resource in enumerate(target['resources']):
            src=Path(resource);dest=work/'resources'/target['id']/str(index)/src.name
            dest.parent.mkdir(parents=True,exist_ok=True)
            if src.is_dir():shutil.copytree(src,dest,symlinks=False)
            else:shutil.copy2(src,dest)
    context={'token':tokens['development'],'models':researcher_view(cfg)['models'],
        'base_url':'http://127.0.0.1:18765','output_dir':'/workspace/client-artifacts',
        **public_contract(config,config['tokens'][tokens['development']])}
    write(work/'access.json',context);(work/'access.json').chmod(0o600)
    return work


def mock_submission(work):
    """Deterministic fixture writer, not an agent or a benchmark-quality result."""
    source=work/'submission';source.mkdir(exist_ok=True)
    (source/'run.py').write_text('''import argparse,json
from pathlib import Path
from research_sdk import Client
p=argparse.ArgumentParser();p.add_argument('--context');p.add_argument('--output');a=p.parse_args()
c=Client(a.context);items=[]
for ident,prompt,answer in [('sum','What is 2 + 2?','4'),('product','What is 2 * 3?','6')]:
    items.append(c.item(ident,prompt,lambda text: float(text.strip()==answer)))
    path=Path(a.output);temp=path.with_suffix('.tmp');temp.write_text(json.dumps({'protocol_version':1,'items':items}));temp.replace(path)
''')
    (source/'README.md').write_text('Synthetic two-item pipeline fixture. Not a research result.\n')
    write(source/'evaluation.json',{'protocol_version':1,'items':[{'id':'sum'},{'id':'product'}],
                                   'selection':'fixed','aggregation':{'kind':'weighted_mean'}})


def session_id(trace):
    ident=None
    for line in (trace/'claude.stdout').read_text().splitlines():
        try:event=json.loads(line)
        except ValueError:continue
        if event.get('session_id'):ident=event['session_id']
    return ident


def reused_jobs(config, models, source):
    fingerprint=hashlib.sha256(json.dumps(digest_tree(source),sort_keys=True).encode()).hexdigest()
    found={}
    for path in sorted((Path(config['run_root'])/'research-jobs').glob('*/request.json'),key=lambda p:p.stat().st_mtime):
        meta=json.loads(path.read_text())
        if meta.get('submission_id')==fingerprint and meta.get('wallet')=='development' and meta.get('model') in models and meta.get('kind')=='suite':
            # Earliest measurement only: no best-of selection of completed answers.
            found.setdefault(meta['model'],{'id':path.parent.name,'submission_id':fingerprint})
    return found


def accounting(out, cfg):
    ledger=Ledger(out/'gateway/ledger.sqlite');wallets=ledger.status()
    with ledger.connect() as db:
        rows=[dict(r) for r in db.execute('SELECT wallet, model, SUM(charged) AS metered_usd, SUM(CASE WHEN charged IS NULL THEN reserve ELSE 0 END) AS outstanding_reserved_usd, COUNT(*) AS calls FROM calls GROUP BY wallet,model')]
        unknown=db.execute('SELECT COUNT(*) FROM calls WHERE charged IS NULL').fetchone()[0]
    prices={m['id']:m['price'] for m in cfg['models']+cfg['researchers']}
    with ledger.connect() as db:
        for row in rows:
            calls=db.execute('SELECT usage,charged FROM calls WHERE wallet=? AND model=?',(row['wallet'],row['model'])).fetchall()
            estimates=[cache_adjusted_cost(json.loads(c['usage'] or '{}'),prices[row['model']]) if c['charged'] is not None else None for c in calls]
            row['cache_adjusted_estimate_usd']=sum(estimates) if all(v is not None for v in estimates) else None
    limits={'designer':cfg['budgets']['researcher_usd'],'development':cfg['budgets']['development_usd'],'evaluation':cfg['budgets']['evaluation_usd']}
    if 'judge_usd' in cfg['budgets']:limits['judge']=cfg['budgets']['judge_usd']
    closed=[w['name'] for w in wallets if w['cap']==0]
    with ledger.connect() as db:
        violations=[dict(r) for r in db.execute('SELECT s.id,s.wallet,s.cap,SUM(c.charged) AS charged FROM budget_scopes s JOIN call_budget_scopes cs ON cs.scope=s.id JOIN calls c ON c.id=cs.call_id GROUP BY s.id HAVING SUM(c.charged)>s.cap+1e-9')]
    within=not closed and not violations and all(w['charged']<=limits[w['name']]+1e-9 for w in wallets)
    return {'wallets':wallets,'by_model':rows,'unknown_calls':unknown,'within_budget':within,
            'status':'settled' if not unknown else 'pending','closed_wallets':closed,'scope_violations':violations,
            'note':'Metered estimates from provider usage and configured prices; reservations are not invoices.'}


def run(config_path, researcher_id, output, *, mock=False):
    cfg=load(config_path)
    candidates=[r for r in cfg['researchers'] if r['id']==researcher_id] if researcher_id else cfg['researchers']
    if len(candidates)!=1:raise ValueError('Choose exactly one configured --researcher ID per output directory')
    researcher=candidates[0]
    if (researcher['harness']=='mock')!=mock:raise ValueError('Mock harness requires --mock; --mock cannot substitute for a real researcher')
    if mock and cfg['design']['minimum_items']!=2:raise ValueError('The mock fixture requires minimum_items: 2')
    doctor(cfg['runtime']['rootfs'])
    if not mock and any('REPLACE' in m['model'] or 'YOUR_' in cfg['providers'][m['provider']]['upstream'] for m in [researcher,*cfg['models']]):raise ValueError('Replace example model and provider placeholders before a paid run')
    if not mock and not shutil.which('claude'):raise ValueError('Install the native Claude Code executable for this harness')
    out=Path(output).resolve();out.mkdir(parents=True,exist_ok=False);out.chmod(0o700)
    process=None;server=None;state={'phase':'preparing','started':time.time(),'researcher':researcher['id'],'mock':mock}
    def update(**values):state.update(values);write(out/'state.json',state)
    def interrupted(signum,frame):raise InterruptedError('Interrupted; existing ledgers and job IDs are preserved')
    prior=signal.signal(signal.SIGTERM,interrupted)
    try:
        frozen_cfg={k:v for k,v in cfg.items() if not k.startswith('_')}
        write(out/'config.resolved.json',frozen_cfg)
        write(out/'provenance.json',{'config_sha256':cfg['_config_sha256'],'reference_sha256':cfg['_reference_hashes'],
            'code_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.iterdir() if p.suffix in ('.py','.c')},'started':state['started'],'mock':mock})
        if mock:server=mock_provider()
        with tempfile.TemporaryDirectory(prefix='seb-experiment-') as sockets:
            config,tokens=build_gateway(cfg,researcher,out,Path(sockets)/'gateway.sock',
                mock_url=f'http://127.0.0.1:{server.server_port}' if server else None)
            process=start_gateway(config,out)
            try:
                if cfg['evaluation']['preflight']:
                    update(phase='preflight')
                    readiness=out/'readiness';readiness.mkdir()
                    mock_submission(readiness)
                    ready=run_jobs(config,tokens['preflight'],[m['id'] for m in cfg['models']],'submission',
                        out/'preflight-jobs',min(time.time()+3600,config['research_deadline_epoch']),pilot=True)
                    write(out/'preflight.json',{'models':len(ready),'transport_complete':all(r.get('score_status')=='valid' for r in ready),
                        'note':'Correctness is not a transport gate; completed empty/wrong answers are retained.'})
                    if not all(r.get('score_status')=='valid' for r in ready):raise RuntimeError('Model transport preflight incomplete; no researcher launched')
                work=prepare_workspace(cfg,config,tokens,out)
                root=out/'researcher-rootfs'
                if not mock:shutil.copytree(cfg['runtime']['rootfs'],root,symlinks=True)
                previous_session=None;checkpoints=[];feedback_file=None
                deadline=config['research_deadline_epoch']
                for index in range(cfg['design']['rounds']):
                    remaining=int(deadline-time.time())
                    if remaining<=0:break
                    update(phase='designing',round=index+1)
                    if mock:mock_submission(work)
                    else:
                        prompt=(DOMAIN_TASK if cfg.get('domain_protocol') else TASK)+f'\nEach checkpoint is limited to {cfg["design"]["checkpoint_seconds"]} seconds.\nThis is round {index+1}/{cfg["design"]["rounds"]}. Remaining design time: {remaining} seconds.\n'
                        if researcher.get('prompt_file'):prompt+='\nOperator task instructions:\n'+Path(researcher['prompt_file']).read_text()
                        if feedback_file:prompt+='\nReview the previous white-box feedback at '+feedback_file+' and revise if useful.\n'
                        trace=out/f'researcher-trace-{index+1}'
                        rc=launch_claude(root,work,trace,config['gateway_socket'],tokens['designer'],researcher['id'],prompt,
                            timeout=min(remaining,cfg['design']['checkpoint_seconds']),effort=researcher.get('effort'),resume_session=previous_session,
                            extra_env={'SEB_CONTEXT':'/workspace/access.json','PYTHONPATH':'/workspace:/opt/science'},
                            extra_binds=[(cfg['runtime']['science_packages'],'/opt/science',True)])
                        check_designer_exit(trace,rc);previous_session=session_id(trace)
                    source=work/'submission';load_manifest(source,cfg['design']['minimum_items'])
                    checkpoint=out/'checkpoints'/f'round-{index+1}';checkpoint.parent.mkdir(exist_ok=True)
                    freeze_program(source,checkpoint)
                    # Reuse exact-snapshot measurements, including complete wrong/empty answers.
                    dev=[m for m in cfg['models'] if m['split']=='development']
                    jobfolder=out/f'development-{index+1}';jobfolder.mkdir()
                    reused=reused_jobs(config,[m['id'] for m in dev],source)
                    for model,job in reused.items():write(jobfolder/(model+'.job.json'),job)
                    update(phase='development_evaluation',round=index+1)
                    results=run_jobs(config,tokens['development'],[m['id'] for m in dev],'submission',jobfolder,deadline)
                    report=score_panel(config,checkpoint,results,dev,config['whitebox']['references'],config['whitebox']['targets'],
                        out/f'development-scores-{index+1}',overall=cfg['overall'],heldout=False)
                    # Give only visible-target feedback to the next round.
                    feedback_file=f'/workspace/feedback-round-{index+1}.json'
                    write(work/f'feedback-round-{index+1}.json',report)
                    checkpoints.append({'round':index+1,'path':str(checkpoint),'whitebox_overall':report['overall']['score']})
                    write(out/'checkpoints.json',checkpoints)
                if not checkpoints:raise RuntimeError('Researcher produced no valid frozen submission before the deadline')
                # Predeclared policy: final round, never chosen with black-box acceptance scores.
                source=Path(checkpoints[-1]['path'])
                acceptance=out/'acceptance-input';acceptance.mkdir()
                freeze_program(source,acceptance/'suite')
                write(out/'freeze.json',{'selected_round':checkpoints[-1]['round'],'policy':'last_valid_round',
                    'files':digest_tree(acceptance/'suite'),'frozen_at':time.time(),
                    'predictor':'submitted' if (source/'predictor.py').is_file() else ('missing_required_predictor' if cfg.get('domain_protocol') else 'fixed_ridge_baseline')})
                # Revoke researcher access before measuring the hidden panel.
                (work/'access.json').unlink(missing_ok=True)
                update(phase='acceptance')
                acceptance_models=[m for m in cfg['models'] if not cfg.get('domain_protocol') or m['split']=='holdout']
                results=run_jobs(config,tokens['evaluation'],[m['id'] for m in acceptance_models],'suite',
                    out/'acceptance-jobs',time.time()+cfg['evaluation']['seconds'])
                update(phase='scoring')
                if cfg.get('domain_protocol'):
                    report=score_domain(config,acceptance/'suite',
                        json.loads((out/'development-1/results.json').read_text()),results,cfg['models'],
                        cfg['_references'],metadata(cfg,'whitebox'),metadata(cfg,'blackbox'),out/'domain',
                        minimum_models=cfg['domain_protocol']['minimum_models'],
                        minimum_families=cfg['domain_protocol']['minimum_families'])
                    reports={'domain':report}
                else:
                    reports={}
                    for visibility in ['whitebox','blackbox']:
                        targets=metadata(cfg,visibility)
                        reports[visibility]=score_panel(config,acceptance/'suite',results,cfg['models'],
                            {t:cfg['_references'][t] for t in targets},targets,out/visibility,overall=cfg['overall'])
                bill=accounting(out,cfg)
                overall=({'metric':'visible_utility','score':reports['domain']['visible_utility'],
                    'sealed_utility':reports['domain']['sealed_utility'],'source':'domain_protocol_v1'}
                    if cfg.get('domain_protocol') else reports['blackbox']['overall'])
                complete=all(r.get('score_status')=='valid' for r in results)
                result={'researcher':researcher['id'],'overall':overall,'complete_models':sum(r.get('score_status')=='valid' for r in results),
                    'expected_models':len(acceptance_models),'accounting':bill,'mock':mock,
                    'eligible':bool(complete and overall['score'] is not None and bill['within_budget'] and bill['unknown_calls']==0),
                    'predictor':json.loads((out/'freeze.json').read_text())['predictor'],
                    'note':('Held-out candidates only; sealed targets use the frozen suite aggregate without target-label fitting.'
                        if cfg.get('domain_protocol') else 'Black-box family CV fits target-specific labels after freezing; it is not zero-shot target prediction.'),
                    **({'paid_api_calls':0,'quality_claim':'none; simulated pipeline fixture'} if mock else {})}
            finally:stop_gateway(process);process=None
            # Reconcile after gateway shutdown so interrupted calls keep their final reservations.
            result['accounting']=accounting(out,cfg)
            result['eligible']=bool(complete and overall['score'] is not None and result['accounting']['within_budget'] and result['accounting']['unknown_calls']==0)
            if cfg.get('domain_protocol'):
                result['visible_utility']=reports['domain']['visible_utility']
                result['sealed_utility']=reports['domain']['sealed_utility']
                result['eligible']=result['eligible'] and result['sealed_utility'] is not None
            write(out/'result.json',result)
            update(phase='completed' if result['eligible'] else 'incomplete',finished=time.time())
    except BaseException as error:
        update(phase='failed',error=type(error).__name__+': '+str(error),finished=time.time());raise
    finally:
        stop_gateway(process)
        if server:server.shutdown();server.server_close()
        for path in out.glob('*.secret'):path.unlink()
        (out/'gateway.private.json').unlink(missing_ok=True)
        (out/'researcher-work/access.json').unlink(missing_ok=True)
        signal.signal(signal.SIGTERM,prior)
    return result
