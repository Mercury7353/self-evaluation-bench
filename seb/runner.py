"""Validate, freeze and independently execute Harbor-format submissions."""
import hashlib, json, math, os, shutil, time, traceback, uuid
from pathlib import Path
from .container import build_task, contained, launch_claude, run_logged, safe_path
from .ledger import Ledger


def digest_tree(root):
    root=Path(root)
    result={}
    for p in sorted(root.rglob('*')):
        if p.is_symlink():raise ValueError(f'Submission symlinks are not supported: {p}')
        if p.is_file():result[str(p.relative_to(root))]=hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def validate(submission):
    from harbor.models.task.task import Task
    root=Path(submission).resolve()
    manifest=json.loads((root/'benchmark.json').read_text())
    tasks=manifest['tasks']
    if not isinstance(tasks,list) or not tasks:raise ValueError('tasks must be non-empty')
    seen=set()
    for entry in tasks:
        path=entry['path'];taskpath=safe_path(root,path)
        if path in seen:raise ValueError('Duplicate task path')
        seen.add(path)
        task=Task(taskpath)
        if task.has_steps:raise ValueError('Multi-step Harbor tasks need a separate backend')
        weight=entry.get('weight',1)
        if not isinstance(weight,(int,float)) or not math.isfinite(weight) or weight<0:raise ValueError('Invalid weight')
        if task.config.environment.gpus:raise ValueError('This run has no GPU')
    if sum(t.get('weight',1) for t in tasks)<=0:raise ValueError('Weights sum to zero')
    return manifest


def freeze(source,dest):
    source,dest=Path(source),Path(dest)
    validate(source)
    before=digest_tree(source)
    shutil.copytree(source,dest)
    after=digest_tree(dest)
    if before!=after:raise ValueError('Submission changed during freeze')
    (dest.parent/(dest.name+'.sha256.json')).write_text(json.dumps(after,indent=2))
    return after


def execute_task(task_dir, model, token, config, run_dir, *, oracle=False, effort=None):
    run_dir=Path(run_dir);run_dir.mkdir(parents=True,exist_ok=True)
    if not oracle and effort is None:
        effort=config.get('efforts',{}).get(model)
    # Snapshot before executing: source edits cannot change an in-flight trial.
    snapshot=run_dir/'task';shutil.copytree(task_dir,snapshot,symlinks=True)
    from harbor.models.task.task import Task
    task=Task(snapshot)
    result={'task':str(task_dir),'model':model,'started':time.time(),'status':'running',
            'task_sha256':digest_tree(snapshot),'effort':effort,'harness':'Claude Code (operator installation)' if not oracle else 'oracle'}
    resultpath=run_dir/'result.json';resultpath.write_text(json.dumps(result,indent=2))
    try:
        root=run_dir/'rootfs'
        built=build_task(snapshot/'environment',root,config['image_cache'],run_dir/'build')
        result['environment']=built
        task_env={**built['env'],**task.config.environment.env}
        work=root/'workspace';work.mkdir(exist_ok=True)
        agent_timeout=min(float(task.config.agent.timeout_sec),config.get('task_timeout',float('inf')))
        verifier_timeout=min(float(task.config.verifier.timeout_sec),config.get('verifier_timeout',float('inf')))
        result.update(agent_timeout_seconds=agent_timeout,verifier_timeout_seconds=verifier_timeout)
        if oracle:
            shutil.copytree(snapshot/'solution',root/'solution',dirs_exist_ok=True)
            result['agent_exit']=run_logged(contained(root,['/bin/bash','/solution/solve.sh'],cwd=built['cwd'],env={**task_env,**task.config.solution.env}),run_dir/'oracle',timeout=agent_timeout)
        else:
            # The task environment is /; /workspace is the Claude home and cwd.
            # Explicitly state the Dockerfile WORKDIR so task semantics are retained.
            instruction=task.instruction+f'\n\nThe task working directory is {built["cwd"]}. Complete the task there.\n'
            result['agent_network']=config.get('candidate_network',False)
            from .execution_policy import output_limit,policy_for
            limit=output_limit(config,config.get('candidate_output_tokens'))
            result['candidate_output_tokens']=limit
            env=dict(task_env)
            if policy_for(config):env.update(CLAUDE_CODE_MAX_RETRIES='0',API_TIMEOUT_MS='4200000')
            result['agent_exit']=launch_claude(root,work,run_dir/'agent',config['gateway_socket'],token,model,instruction,timeout=agent_timeout,effort=effort,extra_env=env,research_network=result['agent_network'],output_tokens=limit)
        # Verifier code is introduced only after the candidate has finished.
        tests=root/'tests'
        if tests.exists():shutil.rmtree(tests)
        shutil.copytree(snapshot/'tests',tests)
        verifier_logs=run_dir/'verifier';verifier_logs.mkdir(exist_ok=True)
        rc=run_logged(contained(root,['/bin/bash','/tests/test.sh'],cwd=built['cwd'],env={**task_env,**task.config.verifier.env},network=config.get('verifier_network',False),
                     binds=[(verifier_logs,'/logs/verifier',False)]),run_dir/'verify',timeout=verifier_timeout)
        result['verifier_exit']=rc
        rewards=verifier_logs/'reward.json'
        txt=verifier_logs/'reward.txt'
        if rewards.exists():
            reward=json.loads(rewards.read_text())
            if isinstance(reward,dict):reward=reward.get('reward',next(iter(reward.values())) if len(reward)==1 else None)
        elif txt.exists():reward=float(txt.read_text().strip())
        else:reward=None
        if not isinstance(reward,(int,float)) or not math.isfinite(reward):raise ValueError('Verifier did not produce a finite reward')
        result['reward']=reward
        result['status']='ok' if rc==0 and result['agent_exit']==0 else 'execution_failed'
        if config.get('evaluation_policy'):
            # A verifier may return exit 1 for failed assertions while writing a
            # valid reward. That is a scored model failure, not a missing trial.
            harness_error=False
            trace=run_dir/'agent/claude.stdout'
            if trace.exists():
                with trace.open() as stream:
                    for line in stream:
                        try:event=json.loads(line)
                        except ValueError:continue
                        if isinstance(event,dict) and event.get('type')=='result':
                            harness_error=bool(event.get('is_error'))
            valid=result['agent_exit']==0 and rc in (0,1) and not harness_error
            result.update(status='ok' if valid else 'execution_failed',
                          score_status='valid' if valid else 'incomplete',
                          execution_status='completed' if valid else 'incomplete')
            if harness_error:result['error']='Candidate harness reported a terminal error'
    except Exception as e:
        result.update(status='error',error=type(e).__name__+': '+str(e))
        (run_dir/'exception.txt').write_text(traceback.format_exc())
    result['finished']=time.time()
    resultpath.write_text(json.dumps(result,indent=2))
    return result


def evaluate(submission,config,output,*,wallet_prefix='evaluate-'):
    submission,output=Path(submission),Path(output);output.mkdir(parents=True,exist_ok=True)
    manifest=validate(submission)
    rows=[]
    for i,(token,entry) in enumerate((x for x in config['tokens'].items() if x[1]['wallet'].startswith(wallet_prefix))):
        model=entry['models'][0];trials=[]
        for j,item in enumerate(manifest['tasks']):
            trial=execute_task(submission/item['path'],model,token,config,output/f'model-{i}'/f'task-{j}',effort=config.get('efforts',{}).get(model))
            trials.append(trial)
        valid=all(t['status']=='ok' for t in trials)
        score=None
        if valid:
            score=sum(t['reward']*item.get('weight',1) for t,item in zip(trials,manifest['tasks']))/sum(item.get('weight',1) for item in manifest['tasks'])
            if (submission/'aggregate.py').exists():
                # Custom aggregation is executed inside an isolated root, never on host.
                aggroot=output/f'model-{i}'/'aggregate-root'
                shutil.copytree(config['base_root'],aggroot,symlinks=True)
                inp=aggroot/'input.json';inp.write_text(json.dumps({'tasks':[{'path':item['path'],'reward':t['reward']} for item,t in zip(manifest['tasks'],trials)]}))
                shutil.copy2(submission/'aggregate.py',aggroot/'aggregate.py')
                rc=run_logged(contained(aggroot,['/usr/local/bin/python','/aggregate.py','/input.json']),output/f'model-{i}'/'aggregate',timeout=60)
                try:
                    if rc:raise ValueError('Aggregator failed')
                    score=json.loads((output/f'model-{i}'/'aggregate.stdout').read_text())['score']
                    if not isinstance(score,(int,float)) or not math.isfinite(score):raise ValueError('Invalid aggregate score')
                except Exception:valid=False;score=None
        wallet=Ledger(Path(config['artifacts'])/'ledger.sqlite').status(entry['wallet'])[0]
        if wallet['outstanding'] or wallet['charged']>entry['cap']+1e-9:valid=False;score=None
        rows.append({'model':model,'score':score,'valid':valid,'wallet':wallet,'trials':trials})
        (output/'scores.json').write_text(json.dumps(rows,indent=2))
    return rows


def correlation(scores,reference):
    from scipy.stats import spearmanr,kendalltau
    if any(not x['valid'] or x.get('score') is None for x in scores):
        return {'status':'incomplete','reason':'At least one candidate failed; no dropping candidates'}
    mapping={x['model']:x['target_score'] for x in reference}
    if any(x['model'] not in mapping for x in scores):return {'status':'missing_reference'}
    x=[a['score'] for a in scores];y=[mapping[a['model']] for a in scores]
    if len(set(x))<2 or len(set(y))<2:return {'status':'non_discriminating','spearman':None,'kendall_tau_b':None}
    return {'status':'exploratory','n':len(x),'spearman':float(spearmanr(x,y).statistic),'kendall_tau_b':float(kendalltau(x,y,variant='b').statistic),
            'reference_protocol':'Caller-provided reference; verify execution protocol comparability'}
