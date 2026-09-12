"""Audit a frozen complete reference panel and compare one proxy to each target."""
import argparse
import hashlib
import itertools
import json
import math
import random
from pathlib import Path

from scipy.stats import kendalltau, spearmanr


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def audit_panel(panel):
    problems = []
    models = panel.get('models', [])
    ids = [model.get('id') for model in models]
    if panel.get('protocol_version') != 1: problems.append('protocol_version must be 1')
    if len(ids) < 3 or any(not isinstance(x,str) or not x for x in ids) or len(set(ids)) != len(ids):
        problems.append('Require at least three distinct model IDs')
    for model in models:
        if not all(model.get(k) for k in ('family','checkpoint','provider')):
            problems.append(f"{model.get('id')}: incomplete model provenance")
    targets = panel.get('targets', [])
    if not targets: problems.append('Missing targets')
    target_ids=[target.get('id') for target in targets]
    if len(set(target_ids)) != len(target_ids): problems.append('Duplicate target IDs')
    result = {}
    for target in targets:
        name = target.get('id', 'unknown')
        task_ids = target.get('task_ids', [])
        repeats = target.get('repeats')
        protocol = target.get('execution_protocol', {})
        if target.get('visibility') not in ('visible', 'hidden'): problems.append(f'{name}: invalid visibility')
        if not task_ids or len(task_ids) != len(set(task_ids)): problems.append(f'{name}: invalid task set')
        if type(repeats) is not int or repeats < 1:
            problems.append(f'{name}: invalid repeats'); continue
        if not all(protocol.get(k) for k in ('dataset_version','dataset_digest','harness','harness_commit','failure_policy','resource_limits')):
            problems.append(f'{name}: incomplete execution protocol')
        if set(target.get('results', {})) != set(ids): problems.append(f'{name}: incomplete/extra model rows')
        scores = {}
        for model in ids:
            row = target.get('results', {}).get(model, {})
            if row.get('protocol_digest') != digest(protocol): problems.append(f'{name}/{model}: protocol mismatch')
            if not row.get('source') or not row.get('artifact_sha256'): problems.append(f'{name}/{model}: missing source/hash')
            trials = row.get('trials', [])
            expected = set(itertools.product(task_ids, range(repeats)))
            actual = [(trial.get('task_id'), trial.get('repeat')) for trial in trials]
            if len(actual) != len(expected) or set(actual) != expected:
                problems.append(f'{name}/{model}: incomplete or duplicate task/repeat coverage')
            valid = True
            for trial in trials:
                score = trial.get('score')
                if trial.get('status') not in ('completed','model_failure'):
                    problems.append(f'{name}/{model}: unresolved execution'); valid=False; break
                if type(score) not in (int,float) or not math.isfinite(score) or not 0 <= score <= 1:
                    problems.append(f'{name}/{model}: invalid score'); valid=False; break
                if trial['status']=='model_failure' and score!=0:
                    problems.append(f'{name}/{model}: nonzero model failure'); valid=False; break
            if valid and trials: scores[model] = sum(t['score'] for t in trials)/len(trials)
        result[name] = scores
    return {'ready':not problems,'problems':problems,'scores':result,'panel_sha256':digest(panel)}


def _correlation(x, y):
    if len(x)<3 or len(set(x))<2 or len(set(y))<2:
        return {'spearman':None,'kendall_tau_b':None}
    return {'spearman':float(spearmanr(x,y).statistic),
            'kendall_tau_b':float(kendalltau(x,y,variant='b').statistic)}


def _pairs(x, y, *, adjacent_only=False):
    pairs=list(itertools.combinations(range(len(x)),2))
    if adjacent_only:
        order=sorted(range(len(y)),key=lambda i:y[i])
        pairs=list(zip(order,order[1:]))
    counts={'concordant':0,'discordant':0,'proxy_ties':0,'target_ties':0}
    for a,b in pairs:
        dx,dy=x[a]-x[b],y[a]-y[b]
        key=('target_ties' if dy==0 else 'proxy_ties' if dx==0 else 'concordant' if dx*dy>0 else 'discordant')
        counts[key]+=1
    denominator=counts['concordant']+counts['discordant']+counts['proxy_ties']
    return counts | {'accuracy_excluding_target_ties':counts['concordant']/denominator if denominator else None}


def compare(panel, proxy_rows, *, seed=0, bootstrap_samples=1000):
    audit=audit_panel(panel)
    if not audit['ready']:return {'status':'reference_incomplete','audit':audit}
    ids=[row['id'] for row in panel['models']]
    proxy={row['model']:row for row in proxy_rows}
    if len(proxy)!=len(proxy_rows) or set(proxy)!=set(ids):
        return {'status':'incomplete','reason':'Proxy panel differs; no dropping or adding models'}
    if any(row.get('score_status','valid' if row.get('valid') else None)!='valid'
           or type(row.get('score')) not in (int,float) or not math.isfinite(row['score']) for row in proxy_rows):
        return {'status':'incomplete','reason':'Every frozen model requires a valid proxy score'}
    x=[proxy[model]['score'] for model in ids]
    families={}
    for i,model in enumerate(panel['models']):families.setdefault(model['family'],[]).append(i)
    targets={}
    for target in panel['targets']:
        y=[audit['scores'][target['id']][model] for model in ids]
        stats=_correlation(x,y)
        rng=random.Random(seed)
        samples={'spearman':[],'kendall_tau_b':[]}
        names=list(families)
        if len(names)>=3:
            for _ in range(bootstrap_samples):
                indices=[i for family in rng.choices(names,k=len(names)) for i in families[family]]
                trial=_correlation([x[i] for i in indices],[y[i] for i in indices])
                for key,value in trial.items():
                    if value is not None:samples[key].append(value)
        intervals={}
        for key,values in samples.items():
            values.sort()
            intervals[key]=([values[int(.025*(len(values)-1))],values[int(.975*(len(values)-1))]] if values else None)
        targets[target['id']]={'visibility':target['visibility'],'n_models':len(ids),'n_families':len(names),
            **stats,'pairs':_pairs(x,y),'adjacent_target_pairs':_pairs(x,y,adjacent_only=True),
            'within_family':{family:{'n':len(indices),'pairs':_pairs([x[i] for i in indices],[y[i] for i in indices])}
                             for family,indices in families.items() if len(indices)>1},
            'family_bootstrap_95_interval':intervals,'bootstrap_valid_samples':{k:len(v) for k,v in samples.items()}}
    return {'status':'exploratory','panel_sha256':audit['panel_sha256'],'proxy_sha256':digest(proxy_rows),
            'seed':seed,'bootstrap_samples':bootstrap_samples,'targets':targets,
            'caution':'Few model families limit uncertainty estimates; cross-model correlation is not RSI improvement.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('panel',type=Path);parser.add_argument('--proxy',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();panel=json.loads(args.panel.read_text())
    result=compare(panel,json.loads(args.proxy.read_text())) if args.proxy else audit_panel(panel)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'output':str(args.output),'ready':result.get('ready'),'status':result.get('status')}))


if __name__=='__main__':main()
