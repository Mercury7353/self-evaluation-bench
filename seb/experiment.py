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
from .codex_harness import launch_codex
from .evaluation import load_manifest, public_contract
from .experiment_config import load, describe, researcher_view
from .gateway import reservation
from .ledger import Ledger
from .runner import digest_tree
from .scoring import score_panel
from .domain_scoring import score_domain, reference_status
from .research_lifecycle import ResearchLifecycle
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

If configured, `auxiliary_models` lists fixed grader/simulator handles separately from candidates.
Inside a suite, call `c.chat(..., model="helper-id", item_id=item_id)` and return its evidence
alongside the candidate evidence. A grader may return `{"score": ..., "answer_status": "answered",
"evidence": [helper_reply["evidence"]]}`; `c.item` merges that list with the candidate call.
All helper calls consume the same development/evaluation wallet and item/suite limits, including
unknown usage reservations. They inherit the frozen output/retry policy. Helper IDs cannot be
suite/agent targets; every completed item must include the evaluated candidate's own evidence.
A grader API failure leaves the item incomplete without repeating the candidate answer.
Record helper prompts, parsing and failure handling in the frozen submission, as with local graders.

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

JOINT_TASK = """You are researching one inexpensive evaluation across coding, co-work, and reasoning.
All visible target definitions and development resources are provided together in whitebox.json/resources.
You have ONE continuous research run and ONE shared development budget. Allocate them yourself.
You may select original tasks or cheap items, synthesize, rewrite, combine methods, or measure adaptively.
Reuse tasks, tools, graders, and measurements across domains when useful. Domain grouping does not
require separate runs or duplicate candidate calls. Only development candidates are accessible now.
Submit one executable measurement, per-visible-target predictions, and three frozen domain scores.
Each held-out candidate runs the whole program once under the shared final cost cap. Each domain score
(higher means better) is compared with undisclosed targets without target-specific fitting or sign changes.
You decide whether and when to test each revision, including the final version. Your predictor may use
visible development labels and your measured item observations. Freeze code, assets, and stopping rules.
CONTRACT.md and the SDK provide executable interfaces; your research strategy and iteration are your choice.
"""

JOINT_CONTRACT = """
# Joint protocol v2

This is one research run across three domains. whitebox.json lists each domain and its visible targets.
The development wallet and time limit are shared. Each final candidate has one whole-program cost cap;
shared item or grader calls are counted once. Small development pilots may cover a subset of domains.

predictor.py is required. Its fit/predict code receives all visible development labels and anonymous
item observations, but no undisclosed target names/labels or held-out labels. Cross-domain features and
predictor_assets/ are allowed. Return predictions keyed by the visible target IDs.

The final evaluation.json must declare domain_aggregations for exactly the three domain IDs in
whitebox.json. Reuse the same declared item in multiple domains without issuing additional calls:
"domain_aggregations": {
  "coding": {"kind":"weighted_mean", "weights":{"example":1}},
  "co-work": {"kind":"weighted_mean", "weights":{"example":1}},
  "reasoning": {"kind":"weighted_mean", "weights":{"example":1}}
}
This is an interface example, not a recommended measurement design. The platform computes these
weighted means from item results. For a custom rule, declare kind="custom", its input "items" list,
and a textual "method"; implement the frozen rule in run.py and return its [0,1] value under
"domain_scores": {domain_id: value}. Unresolved selected input items make that domain incomplete.
Adaptive exclusion follows the existing frozen selection rule. Missing custom outputs are failures,
not constant predictions. Each domain's two undisclosed targets use exactly its one frozen score.

Final acceptance reports per-target results and separate visible/sealed utility, averaged equally
across domains. It never trains a new predictor on undisclosed labels or treats domains as independent runs.

Keep submission/ in a complete, runnable state, including predictor.py. The controller saves
structurally valid snapshots about every five seconds and at normal exit. If your research time or
researcher-call budget runs out, it freezes the latest valid saved snapshot, not the highest-scoring
version. Partial edits are retained for audit. Validation of a snapshot does not establish correctness.
No new development calls are submitted after the research deadline; existing matching jobs are reused.
"""


def metadata(cfg, visibility):
    return {t['id']:{'scale':t['scale'],'status':t.get('reference_status','primary_archival'),
        **({'domain':t['domain']} if 'domain' in t else {})} for t in cfg['benchmarks'][visibility]}


def joint_domains(cfg):
    protocol=cfg.get('domain_protocol',{})
    if protocol.get('version') not in (2,3):return None
    return {d:{v:[t['id'] for t in cfg['benchmarks'][key] if t['domain']==d]
        for v,key in [('visible','whitebox'),('sealed','blackbox')]} for d in protocol['domains']}


def build_gateway(cfg, researcher, out, socket, *, mock_url=None):
    from .experiment_config import auxiliary_view
    auxiliary=cfg.get('auxiliary_models',[])
    helper_ids=[m['id'] for m in auxiliary]
    available=[m for m in cfg['models'] if m.get('availability')!='pending']
    all_models=([researcher] if researcher else [])+available+auxiliary
    used={m['provider'] for m in all_models}
    secretfiles={}
    for pid in sorted(used):
        provider=cfg['providers'][pid]
        key='local-mock-only' if mock_url else os.environ.get(provider['key_env'],'')
        if not key:raise ValueError('Missing provider credential environment variable: '+provider['key_env'])
        secret=out/(pid+'.secret');secret.write_text(key);secret.chmod(0o600);secretfiles[pid]=str(secret)
    roles=(['designer'] if researcher else [])+['development','evaluation']
    tokens={name:secrets.token_hex(32) for name in roles}
    development=[m['id'] for m in cfg['models'] if m['split']=='development']
    first_provider=all_models[0]['provider'];first=cfg['providers'][first_provider]
    budget=cfg['budgets']
    config={'artifacts':str(out/'gateway'),'run_root':str(out),'gateway_socket':str(socket),
        'base_root':cfg['runtime']['rootfs'],'science_packages':cfg['runtime']['science_packages'],
        'image_cache':cfg['runtime'].get('image_cache',str(out/'image-cache')),
        'verifier_network':cfg['runtime'].get('verifier_network',False),'key_file':secretfiles[first_provider],
        'upstream':mock_url or first['upstream'],
        'prices':{m['id']:m['price'] for m in all_models},
        'model_backends':{m['id']:{'allow_unreconciled_usage':bool(m.get('allow_unreconciled_usage',False)),'model':m['model'],'key_file':secretfiles[m['provider']],
                            'upstream':mock_url or cfg['providers'][m['provider']]['upstream'],
                            **({'wire_api':cfg['providers'][m['provider']]['wire_api'],'effort':m['effort'],'native_limits':m['native_limits']}
                               if cfg['providers'][m['provider']].get('wire_api') in ('openai_responses','chat_completions') else {})} for m in all_models},
        'efforts':{m['id']:m['effort'] for m in all_models if m.get('effort')},
        'auxiliary_models':helper_ids,'auxiliary_model_info':auxiliary_view(cfg),
        'allow_empty_development_predictor':cfg.get('domain_protocol',{}).get('version')==3,'evaluation_policy':cfg['evaluation']['policy'],'minimum_items':cfg['design']['minimum_items'],
        'require_item_budgets':True,'allow_pilots':True,'suite_cost_cap_usd':budget['suite_usd'],
        'item_cost_cap_usd':budget['item_usd'],'suite_cost_goal_usd':budget['suite_usd'],
        'suite_timeout':cfg['evaluation']['seconds'],'suite_concurrency':cfg['evaluation']['model_concurrency'],
        'request_concurrency_per_model':cfg['evaluation']['requests_per_model'],
        'max_pending_suites':max(len(cfg['models'])*2,8),'reservation_wait_seconds':15,
        'research_deadline_epoch':time.time()+cfg['design']['seconds'],
        'overall':cfg['overall'],
        **({'joint_domains':list(joint_domains(cfg))} if joint_domains(cfg) else {}),
        'whitebox':{'models':[m for m in cfg['models'] if m['split']=='development'],
            'references':{t:row['scores'] for t,row in researcher_view(cfg)['targets'].items()},
            'targets':metadata(cfg,'whitebox')},
        'tokens':{tokens['development']:{'wallet':'development','cap':budget['development_usd'],
                'models':development+helper_ids,'candidate_models':development,
                'workspace':str(out/'researcher-work'),'research':True,'deadline_epoch':time.time()+cfg['design']['seconds']},
            tokens['evaluation']:{'wallet':'evaluation','cap':budget['evaluation_usd'],
                'models':[m['id'] for m in available if not cfg.get('domain_protocol') or m['split']=='holdout'],'workspace':str(out/'acceptance-input'),'allow_suite':True,'start_deadline_on_first_suite':cfg['evaluation']['seconds']}}}
    if researcher:
        config['tokens'][tokens['designer']]={'wallet':'designer','cap':budget['researcher_usd'],
            'models':[researcher['id']],'deadline_epoch':config['research_deadline_epoch']}
    if budget.get('judge_usd'):
        config['tokens'][secrets.token_hex(32)]={'wallet':'judge','cap':budget['judge_usd'],'models':[]}
    if cfg.get('response_cache'):
        from .response_cache import validate_config
        config['response_cache']=validate_config(cfg['response_cache'],[out,
            cfg['runtime']['rootfs'],cfg['runtime']['science_packages']])
    if researcher and researcher['harness']=='codex':
        config['native_researcher']={'id':researcher['id'],'model':researcher['model'],
            'effort':researcher['effort'],**researcher['native_limits']}
        config['tokens'][tokens['designer']]['native_responses']=True
    if cfg['evaluation']['preflight']:
        tokens['preflight']=secrets.token_hex(32)
        config['tokens'][tokens['preflight']]={'wallet':'development','cap':budget['development_usd'],
            'models':[m['id'] for m in available]+helper_ids,
            'candidate_models':[m['id'] for m in available],'research':True,'workspace':str(out/'readiness'),
            'deadline_epoch':config['research_deadline_epoch']}
    for model in all_models:reservation({'max_tokens':cfg['evaluation']['policy']['default_output_tokens'],'messages':[]},model['price'])
    return config,tokens


def prepare_workspace(cfg, config, tokens, out, *, resume=False):
    if resume:
        work=out/'researcher-work'
        if not work.is_dir():raise ValueError('Original researcher workspace is missing')
        write_access(cfg,config,tokens,work)
        return work
    work=out/'researcher-work';work.mkdir()
    write(work/'whitebox.json',researcher_view(cfg))
    contract=CONTRACT
    if cfg.get('domain_protocol'):
        contract=CONTRACT.split('The platform reports raw-score correlations')[0]
        contract=contract.replace('Optional `predictor.py` contract:', 'Required `predictor.py` contract:')+(JOINT_CONTRACT if joint_domains(cfg) else DOMAIN_CONTRACT)
    (work/'CONTRACT.md').write_text(contract)
    shutil.copy2(Path(__file__).with_name('research_sdk.py'),work/'research_sdk.py')
    for target in cfg['benchmarks']['whitebox']:
        for index,resource in enumerate(target['resources']):
            src=Path(resource);dest=work/'resources'/target['id']/str(index)/src.name
            dest.parent.mkdir(parents=True,exist_ok=True)
            if src.is_dir():shutil.copytree(src,dest,symlinks=False)
            else:shutil.copy2(src,dest)
    write_access(cfg,config,tokens,work)
    return work


def write_access(cfg, config, tokens, work):
    context={'token':tokens['development'],'models':researcher_view(cfg)['models'],
        'efforts':{m:config.get('efforts',{}).get(m) for m in config['tokens'][tokens['development']]['models']},
        'base_url':'http://127.0.0.1:18765','output_dir':'/workspace/client-artifacts',
        **public_contract(config,config['tokens'][tokens['development']])}
    write(work/'access.json',context);(work/'access.json').chmod(0o600)


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
    inherited_path=out/'ledger-reuse.json'
    inherited=json.loads(inherited_path.read_text()) if inherited_path.exists() else None
    inherited_ids=set(inherited['call_ids']) if inherited else set()
    with ledger.connect() as db:
        rows=[dict(r) for r in db.execute('SELECT wallet, model, SUM(charged) AS metered_usd, SUM(CASE WHEN charged IS NULL THEN reserve ELSE 0 END) AS outstanding_reserved_usd, COUNT(*) AS calls FROM calls GROUP BY wallet,model')]
        unknown=db.execute('SELECT COUNT(*) FROM calls WHERE charged IS NULL').fetchone()[0]
    prices={m['id']:m['price'] for m in cfg['models']+cfg['researchers']+cfg.get('auxiliary_models',[]) if m.get('availability')!='pending'}
    with ledger.connect() as db:
        for row in rows:
            calls=db.execute('SELECT c.id,c.usage,c.charged,r.call_id AS replay FROM calls c LEFT JOIN cache_replays r ON r.call_id=c.id WHERE wallet=? AND model=?',(row['wallet'],row['model'])).fetchall()
            # Historical charges remain exact. Do not reprice old usage using a
            # new run's price table, even if the logical model ID is unchanged.
            estimates=[]
            for c in calls:
                if c['replay']:
                    estimates.append(0.)
                elif c['charged'] is None or c['id'] in inherited_ids or row['model'] not in prices:
                    estimates.append(None)
                elif c['charged']==0:
                    estimates.append(0.)
                else:
                    estimates.append(cache_adjusted_cost(json.loads(c['usage'] or '{}'),prices[row['model']]))
            row['equivalent_charge_usd']=row['metered_usd']
            row['provider_metered_usd']=sum(c['charged'] or 0. for c in calls if not c['replay'])
            row['response_cache_hits']=sum(bool(c['replay']) for c in calls)
            row['cache_adjusted_estimate_usd']=sum(estimates) if all(v is not None for v in estimates) else None
            row['cache_adjusted_known_component_usd']=sum(v for v in estimates if v is not None)
            row['inherited_calls']=sum(c['id'] in inherited_ids for c in calls)
    limits={'designer':cfg['budgets']['researcher_usd'],'development':cfg['budgets']['development_usd'],'evaluation':cfg['budgets']['evaluation_usd']}
    if 'judge_usd' in cfg['budgets']:limits['judge']=cfg['budgets']['judge_usd']
    closed=[w['name'] for w in wallets if w['cap']==0]
    with ledger.connect() as db:
        violations=[dict(r) for r in db.execute('SELECT s.id,s.wallet,s.cap,SUM(c.charged) AS charged FROM budget_scopes s JOIN call_budget_scopes cs ON cs.scope=s.id JOIN calls c ON c.id=cs.call_id GROUP BY s.id HAVING SUM(c.charged)>s.cap+1e-9')]
    within=not closed and not violations and all(w['name'] in limits and w['charged']+w['outstanding']<=limits[w['name']]+1e-9 for w in wallets)
    return {'wallets':wallets,'by_model':rows,'unknown_calls':unknown,'within_budget':within,
            'status':'settled' if not unknown else 'pending','closed_wallets':closed,'scope_violations':violations,
            **({'inherited_development':inherited} if inherited else {}),
            'note':'charged/metered_usd are equivalent quota including response replays. provider_metered_usd excludes replay charges; cache_adjusted_estimate_usd additionally applies provider prompt-cache pricing. Unknown reservations remain outstanding. None are invoices.'}


def run(config_path, researcher_id, output, *, mock=False, resume_prelaunch=False, resume_research=False,
        migration_handoff=None, resume_verified_preflight=False):
    if sum([resume_prelaunch,resume_research,resume_verified_preflight])>1:raise ValueError("Choose one recovery mode")
    if resume_prelaunch and resume_research:raise ValueError('Choose one recovery mode')
    if migration_handoff and not resume_research:raise ValueError('Migration must preserve the research context')
    cfg=load(config_path)
    candidates=[r for r in cfg['researchers'] if r['id']==researcher_id] if researcher_id else cfg['researchers']
    if len(candidates)!=1:raise ValueError('Choose exactly one configured --researcher ID per output directory')
    researcher=candidates[0]
    if (researcher['harness']=='mock')!=mock:raise ValueError('Mock harness requires --mock; --mock cannot substitute for a real researcher')
    if mock and cfg['design']['minimum_items']!=2:raise ValueError('The mock fixture requires minimum_items: 2')
    doctor(cfg['runtime']['rootfs'])
    if not mock and any('REPLACE' in m['model'] or 'YOUR_' in cfg['providers'][m['provider']]['upstream'] for m in [researcher,*cfg['models'],*cfg.get('auxiliary_models',[])] if m.get('availability')!='pending'):raise ValueError('Replace example model and provider placeholders before a paid run')
    if not mock and researcher['harness']=='claude_code' and not shutil.which('claude'):
        raise ValueError('Install the native Claude Code executable for this harness')
    if researcher['harness']=='codex':
        from .codex_harness import runtime_files
        runtime_files(cfg['runtime'].get('codex_binary'))
    out=Path(output).resolve();recovery=None;continuation=None
    if resume_prelaunch:
        from .prelaunch_recovery import archive_unstarted,restore_ledger
        recovery=archive_unstarted(out,cfg,researcher['id'])
    if resume_verified_preflight:
        from .prelaunch_recovery import archive_verified_preflight,restore_ledger
        recovery=archive_verified_preflight(out,cfg,researcher['id'])
    if resume_research:
        from .research_continuation import prepare_continuation
        continuation=prepare_continuation(out,cfg,researcher,handoff=migration_handoff)
    else:out.mkdir(parents=True,exist_ok=False)
    out.chmod(0o700)
    process=None;server=None;state={'phase':'preparing','started':time.time(),'researcher':researcher['id'],'mock':mock}
    if recovery:
        state.update(started=recovery['original_started'],prelaunch_recovery=recovery)
        restore_ledger(recovery,out)
    if continuation:state.update(started=continuation['original_started'],research_continuation=continuation)
    def update(**values):state.update(values);write(out/'state.json',state)
    def interrupted(signum,frame):raise InterruptedError('Interrupted; existing ledgers and job IDs are preserved')
    prior=signal.signal(signal.SIGTERM,interrupted)
    try:
        frozen_cfg={k:v for k,v in cfg.items() if not k.startswith('_')}
        update(phase='preparing')
        if not continuation:write(out/'config.resolved.json',frozen_cfg)
        provenance_root=Path(continuation['directory']) if continuation else out
        write(provenance_root/('provenance.continued.json' if continuation else 'provenance.json'),{'config_sha256':cfg['_config_sha256'],'reference_sha256':cfg['_reference_hashes'],
            'code_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.iterdir() if p.suffix in ('.py','.c')},'started':state['started'],'mock':mock})
        if mock:server=mock_provider()
        with tempfile.TemporaryDirectory(prefix='seb-experiment-') as sockets:
            config,tokens=build_gateway(cfg,researcher,out,Path(sockets)/'gateway.sock',
                mock_url=f'http://127.0.0.1:{server.server_port}' if server else None)
            original_clock=continuation or recovery
            if original_clock:
                config['research_deadline_epoch']=original_clock['original_deadline_epoch']
                for entry in config['tokens'].values():
                    if 'deadline_epoch' in entry:entry['deadline_epoch']=original_clock['original_deadline_epoch']
            process=start_gateway(config,out)
            try:
                if cfg['evaluation']['preflight'] and not continuation and not (recovery and recovery.get('preflight_offline_verified')):
                    update(phase='preflight')
                    readiness=out/'readiness';readiness.mkdir()
                    mock_submission(readiness)
                    ready=run_jobs(config,tokens['preflight'],[m['id'] for m in cfg['models'] if m.get('availability')!='pending'],'submission',
                        out/'preflight-jobs',min(time.time()+3600,config['research_deadline_epoch']),pilot=True)
                    write(out/'preflight.json',{'models':len(ready),'transport_complete':all(r.get('score_status')=='valid' for r in ready),
                        'note':'Correctness is not a transport gate; completed empty/wrong answers are retained.'})
                    if not all(r.get('score_status')=='valid' for r in ready):raise RuntimeError('Model transport preflight incomplete; no researcher launched')
                work=prepare_workspace(cfg,config,tokens,out,resume=bool(continuation))
                root=out/'researcher-rootfs'
                if not mock and not continuation:shutil.copytree(cfg['runtime']['rootfs'],root,symlinks=True)
                previous_session=continuation['thread_id'] if continuation else None
                checkpoints=[];feedback_file=None
                deadline=config['research_deadline_epoch']
                lifecycle=ResearchLifecycle(config,work,out,researcher['id'],
                    minimum_items=cfg['design']['minimum_items'],require_predictor=bool(cfg.get('domain_protocol')),
                    resume=bool(continuation),started=state['started'] if continuation else None)
                research_outcome=None
                for index in range(cfg['design']['rounds']):
                    remaining=int(deadline-time.time())
                    if remaining<=0:break
                    update(phase='designing',round=index+1)
                    trace=out/(continuation['trace_directory'] if continuation else f'researcher-trace-{index+1}')
                    if mock:
                        mock_submission(work)
                        rc=0
                    else:
                        prompt=(JOINT_TASK if joint_domains(cfg) else DOMAIN_TASK if cfg.get('domain_protocol') else TASK)
                        prompt+=f'\nRemaining total research time: {remaining} seconds.\n' if cfg.get('domain_protocol') else f'\nEach checkpoint is limited to {cfg["design"]["checkpoint_seconds"]} seconds.\nThis is round {index+1}/{cfg["design"]["rounds"]}. Remaining design time: {remaining} seconds.\n'
                        if researcher.get('prompt_file'):prompt+='\nOperator task instructions:\n'+Path(researcher['prompt_file']).read_text()
                        if feedback_file:prompt+='\nReview the previous white-box feedback at '+feedback_file+' and revise if useful.\n'
                        if continuation:
                            prompt+='\nContinue this same research after an operator infrastructure interruption. Your SDK context, workspace and prior jobs are preserved. Reopen Client() from access.json for refreshed scoped authentication; keep using existing job IDs and completed measurements. The original budget and deadline are unchanged.\n'
                        common={'timeout':min(remaining,cfg['design']['checkpoint_seconds']),
                            'effort':researcher.get('effort'),
                            'extra_env':{'SEB_CONTEXT':'/workspace/access.json','PYTHONPATH':'/workspace:/opt/science'},
                            'extra_binds':[(cfg['runtime']['science_packages'],'/opt/science',True)],
                            'stop_requested':lifecycle.poll}
                        if researcher['harness']=='codex':
                            rc=launch_codex(root,work,trace,config['gateway_socket'],tokens['designer'],researcher['model'],prompt,
                                binary=cfg['runtime'].get('codex_binary'),resume_thread=previous_session,**common)
                        else:
                            rc=launch_claude(root,work,trace,config['gateway_socket'],tokens['designer'],researcher['id'],prompt,
                                resume_session=previous_session,**common)
                    research_outcome=lifecycle.finish(trace,rc,harness=researcher['harness'])
                    if research_outcome['reason'] in ('upstream_blocked','researcher_accounting_guard'):
                        raise RuntimeError('Research stopped: '+research_outcome['reason']+'; saved snapshots and ledger preserved')
                    if not mock and not research_outcome['expected_resource_stop']:
                        try:check_designer_exit(trace,rc,harness=researcher['harness'])
                        except RuntimeError:
                            research_outcome['reason']='harness_error'
                            write(out/'research-checkpoints/outcome.json',research_outcome)
                            raise
                        if researcher['harness']=='codex':
                            previous_session=json.loads((trace/'thread.json').read_text())['thread_id']
                        else:previous_session=session_id(trace)
                    selected=lifecycle.select();source=Path(selected['path'])
                    checkpoint=out/'checkpoints'/f'round-{index+1}';checkpoint.parent.mkdir(exist_ok=True)
                    freeze_program(source,checkpoint)
                    # Use exactly the saved version even if the live directory has partial edits.
                    selected_path=f'controller-selection-{index+1}'
                    freeze_program(checkpoint,work/selected_path)
                    # Reuse exact-snapshot measurements, including complete wrong/empty answers.
                    dev=[m for m in cfg['models'] if m['split']=='development']
                    jobfolder=out/f'development-{index+1}';jobfolder.mkdir()
                    reused=reused_jobs(config,[m['id'] for m in dev],source)
                    for model,job in reused.items():write(jobfolder/(model+'.job.json'),job)
                    update(phase='development_evaluation',round=index+1)
                    if cfg['design'].get('final_development_measurement',True):
                        results=run_jobs(config,tokens['development'],[m['id'] for m in dev],selected_path,jobfolder,deadline)
                    else:
                        results=run_jobs(config,tokens['development'],list(reused),selected_path,jobfolder,deadline)
                        write(jobfolder/'results.json',results)
                    report=score_panel(config,checkpoint,results,dev,config['whitebox']['references'],config['whitebox']['targets'],
                        out/f'development-scores-{index+1}',overall=cfg['overall'],heldout=False)
                    # Give only visible-target feedback to the next round.
                    feedback_file=f'/workspace/feedback-round-{index+1}.json'
                    write(work/f'feedback-round-{index+1}.json',report)
                    checkpoints.append({'round':index+1,'path':str(checkpoint),'whitebox_overall':report['overall']['score'],
                        'research_snapshot':selected['sequence'],'research_outcome':research_outcome})
                    write(out/'checkpoints.json',checkpoints)
                    if research_outcome['reason']=='checkpoint_time_limit':
                        # Legacy multi-round mode has per-round time limits inside one total window.
                        lifecycle.reason=None;lifecycle.evidence=None
                    elif research_outcome['expected_resource_stop']:break
                if not checkpoints:raise RuntimeError('Researcher produced no valid frozen submission before the deadline')
                # Predeclared policy: final round, never chosen with black-box acceptance scores.
                source=Path(checkpoints[-1]['path'])
                acceptance=out/'acceptance-input';acceptance.mkdir()
                freeze_program(source,acceptance/'suite')
                write(out/'freeze.json',{'selected_round':checkpoints[-1]['round'],'policy':'last_valid_round',
                    'files':digest_tree(acceptance/'suite'),'frozen_at':time.time(),
                    'research_snapshot':checkpoints[-1]['research_snapshot'],'research_outcome':research_outcome,
                    'predictor':'submitted' if (source/'predictor.py').is_file() else ('missing_required_predictor' if cfg.get('domain_protocol') else 'fixed_ridge_baseline')})
                # Revoke researcher access before measuring the hidden panel.
                (work/'access.json').unlink(missing_ok=True)
                update(phase='acceptance')
                acceptance_models=[m for m in cfg['models'] if not cfg.get('domain_protocol') or m['split']=='holdout']
                pending_models={m['id']:m['pending_reason'] for m in acceptance_models if m.get('availability')=='pending'}
                results=run_jobs(config,tokens['evaluation'],[m['id'] for m in acceptance_models if m['id'] not in pending_models],'suite',
                    out/'acceptance-jobs',time.time()+cfg['evaluation']['seconds'])
                update(phase='scoring')
                if cfg.get('domain_protocol'):
                    report=score_domain(config,acceptance/'suite',
                        json.loads((out/'development-1/results.json').read_text()),results,cfg['models'],
                        cfg['_references'],metadata(cfg,'whitebox'),metadata(cfg,'blackbox'),out/'domain',
                        minimum_models=cfg['domain_protocol']['minimum_models'],
                        minimum_families=cfg['domain_protocol']['minimum_families'],domains=joint_domains(cfg),
                        reference_panels=cfg['_reference_panels'])
                    reports={'domain':report}
                else:
                    reports={}
                    for visibility in ['whitebox','blackbox']:
                        targets=metadata(cfg,visibility)
                        reports[visibility]=score_panel(config,acceptance/'suite',results,cfg['models'],
                            {t:cfg['_references'][t] for t in targets},targets,out/visibility,overall=cfg['overall'])
                bill=accounting(out,cfg)
                overall=({'metric':'visible_utility','score':reports['domain']['visible_utility'],
                    'sealed_utility':reports['domain']['sealed_utility'],
                    'source':'joint_protocol_v2' if joint_domains(cfg) else 'domain_protocol_v1'}
                    if cfg.get('domain_protocol') else reports['blackbox']['overall'])
                available_complete=len(results)==len(acceptance_models)-len(pending_models) and all(r.get('score_status')=='valid' for r in results)
                complete=available_complete and not pending_models
                result={'researcher':researcher['id'],'overall':overall,'complete_models':sum(r.get('score_status')=='valid' for r in results),
                    'research_outcome':research_outcome,
                    'expected_models':len(acceptance_models),'accounting':bill,'mock':mock,
                    'pending_provider_models':pending_models,'available_measurement_complete':available_complete,
                    'eligible':bool(complete and overall['score'] is not None and bill['within_budget'] and bill['unknown_calls']==0),
                    'predictor':json.loads((out/'freeze.json').read_text())['predictor'],
                    'note':('One shared measurement per held-out candidate; each domain score is reused for both sealed targets without fitting.' if joint_domains(cfg) else 'Held-out candidates only; sealed targets use the frozen suite aggregate without target-label fitting.'
                        if cfg.get('domain_protocol') else 'Black-box family CV fits target-specific labels after freezing; it is not zero-shot target prediction.'),
                    **({'paid_api_calls':0,'quality_claim':'none; simulated pipeline fixture'} if mock else {})}
            finally:stop_gateway(process);process=None
            # Reconcile after gateway shutdown so interrupted calls keep their final reservations.
            result['accounting']=accounting(out,cfg)
            result['eligible']=bool(complete and overall['score'] is not None and result['accounting']['within_budget'] and result['accounting']['unknown_calls']==0)
            if cfg.get('domain_protocol'):
                result.update(reference_status(reports['domain']),measurement_complete=complete)
                result['visible_utility']=reports['domain']['visible_utility']
                result['sealed_utility']=reports['domain']['sealed_utility']
                result['eligible']=result['eligible'] and result['sealed_utility'] is not None
                if joint_domains(cfg):
                    result.update(domains=reports['domain']['domains'],research_unit='joint',
                        complete_domains=sum(all(row['status'] in ('OK','CONST')
                            for visibility in ('visible','sealed') for row in report[visibility].values())
                            for report in reports['domain']['domains'].values()))
            write(out/'result.json',result)
            update(phase='completed' if result['eligible'] else
                'pending_provider' if pending_models and available_complete and not result.get('has_submission_failure') and
                result['accounting']['within_budget'] and result['accounting']['unknown_calls']==0 else
                'pending_reference' if result.get('reference_status')=='pending' and not result['has_submission_failure'] and complete and
                result['accounting']['within_budget'] and result['accounting']['unknown_calls']==0 else 'incomplete',finished=time.time())
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
