"""Platform-owned suite contract and score/execution/accounting separation."""
import json
import math
from pathlib import Path


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def load_manifest(source, minimum_items=100, *, domains=None):
    data = json.loads((Path(source)/'evaluation.json').read_text())
    if data.get('protocol_version') != 1:
        raise ValueError('evaluation.json requires protocol_version=1')
    items = data.get('items', [])
    if not isinstance(items, list) or len(items) < minimum_items:
        raise ValueError(f'evaluation.json requires at least {minimum_items} declared items')
    ids = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']:
            raise ValueError('Declared item IDs must be nonempty strings')
        ids.append(item['id'])
        if 'budget_group' in item and (not isinstance(item['budget_group'],str) or not item['budget_group']):
            raise ValueError('Budget groups must be nonempty strings')
        if not finite(item.get('weight', 1)) or item.get('weight', 1) <= 0:
            raise ValueError('Item weights must be positive and finite')
    if len(set(ids)) != len(ids): raise ValueError('Duplicate declared item IDs')
    if data.get('aggregation', {}).get('kind') not in ('weighted_mean', 'custom'):
        raise ValueError('Declare weighted_mean or custom aggregation')
    if data.get('selection', 'fixed') not in ('fixed', 'adaptive'):
        raise ValueError('Declare fixed or adaptive selection')
    if data.get('selection') == 'adaptive' or data['aggregation']['kind'] == 'custom':
        if not isinstance(data.get('method'), str) or not data['method'].strip():
            raise ValueError('Describe the frozen custom aggregation/adaptive rule in method')
    aggregations=data.get('domain_aggregations',{})
    if not isinstance(aggregations,dict):raise ValueError('domain_aggregations must be a mapping')
    if domains is not None and set(aggregations)!=set(domains):
        raise ValueError('Declare an aggregation for every required domain')
    for domain, rule in aggregations.items():
        if not isinstance(domain,str) or not domain or not isinstance(rule,dict):
            raise ValueError('Invalid domain aggregation')
        if rule.get('kind')=='weighted_mean':
            weights=rule.get('weights')
            if not isinstance(weights,dict) or not weights or set(weights)-set(ids):
                raise ValueError('Domain weights must name declared items')
            if any(not finite(w) or w<=0 for w in weights.values()):
                raise ValueError('Domain weights must be finite and positive')
        elif rule.get('kind')=='custom':
            inputs=rule.get('items')
            if not isinstance(inputs,list) or not inputs or len(inputs)!=len(set(inputs)) or set(inputs)-set(ids):
                raise ValueError('Custom domain aggregation must declare its input items')
            if not isinstance(rule.get('method'),str) or not rule['method'].strip():
                raise ValueError('Describe the frozen custom domain aggregation')
        else:raise ValueError('Domain aggregation must be weighted_mean or custom')
    return data


def domain_outputs(raw, manifest):
    """Reuse validated item measurements, never issue additional model calls."""
    rules=manifest.get('domain_aggregations',{})
    reported=raw.get('domain_scores',{})
    if not isinstance(reported,dict) or set(reported)-set(rules):
        raise ValueError('Domain scores must use frozen domain IDs')
    by_id={item['id']:item for item in raw['items']}
    scores,statuses={},{}
    for domain, rule in rules.items():
        names=rule['weights'] if rule['kind']=='weighted_mean' else rule['items']
        selected=[by_id[n] for n in names if by_id[n]['execution_status']!='not_selected']
        if not selected or any(i['execution_status']!='completed' for i in selected):
            scores[domain]=None;statuses[domain]='incomplete';continue
        if rule['kind']=='weighted_mean':
            value=sum(rule['weights'][i['id']]*i['score'] for i in selected)/sum(rule['weights'][i['id']] for i in selected)
            if domain in reported and (not finite(reported[domain]) or not math.isclose(reported[domain],value,abs_tol=1e-8)):
                raise ValueError('Reported domain score disagrees with frozen weights')
        else:
            value=reported.get(domain)
            if not finite(value) or not 0<=value<=1:
                scores[domain]=None;statuses[domain]='missing_or_invalid';continue
        scores[domain]=value;statuses[domain]='valid'
    return {'domain_scores':scores,'domain_score_status':statuses}


def check_evidence(item, ledger, wallet, started, jobs_root, *, completed):
    evidence = item.get('evidence', [])
    if not isinstance(evidence, list) or (completed and not evidence):
        raise ValueError('Evaluated items require call/agent evidence')
    with ledger.connect() as db:
        for ev in evidence:
            ident = ev.get('id', '')
            if not isinstance(ident, str) or len(ident) != 32 or any(c not in '0123456789abcdef' for c in ident):
                raise ValueError('Invalid evidence ID')
            if ev.get('kind') == 'llm':
                row = db.execute('SELECT * FROM calls WHERE id=?', (ident,)).fetchone()
                if not row or row['wallet'] != wallet or row['created'] < started:
                    raise ValueError('Evidence outside this execution/wallet')
                # Known answer delivery is distinct from whether billing usage arrived.
                if completed and row['state'] != 'completed':
                    raise ValueError('Completed item requires a completed response')
            elif ev.get('kind') == 'agent':
                folder = Path(jobs_root)/ident
                request = json.loads((folder/'request.json').read_text())
                result = json.loads((folder/'result.json').read_text())
                if request['wallet'] != wallet or request['created'] < started:
                    raise ValueError('Agent evidence outside this execution/wallet')
                if completed and result.get('status') != 'ok':
                    raise ValueError('Agent execution did not complete')
            else:
                raise ValueError('Unknown evidence kind')


def normalize_result(raw, manifest, ledger, wallet, started, jobs_root):
    items = raw.get('items')
    if raw.get('protocol_version') != 1 or not isinstance(items, list):
        raise ValueError('Result requires protocol_version=1 and items')
    declared = {item['id']: item for item in manifest['items']}
    if any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in items):
        raise ValueError('Invalid result item')
    if len(items) != len(declared) or {item['id'] for item in items} != set(declared):
        raise ValueError('Return each declared item exactly once, including missing/unexecuted items')
    weighted = total = 0.
    complete, attempted, selected = True, 0, 0
    for item in items:
        execution = item.get('execution_status')
        answer = item.get('answer_status')
        score = item.get('score')
        if execution not in ('completed', 'infra_error', 'not_run', 'budget_exhausted', 'not_selected'):
            raise ValueError('Invalid item execution_status')
        if answer not in ('answered', 'missing', 'invalid', 'refused', 'not_applicable'):
            raise ValueError('Invalid item answer_status')
        if execution == 'not_selected':
            if manifest.get('selection') != 'adaptive' or score is not None or answer != 'not_applicable':
                raise ValueError('Only a frozen adaptive rule may exclude an unselected item')
            if item.get('evidence'): raise ValueError('An unselected item cannot have execution evidence')
            continue
        selected += 1
        weight = declared[item['id']].get('weight', 1)
        total += weight
        if execution == 'completed':
            attempted += 1
            if not finite(score) or not 0 <= score <= 1:
                raise ValueError('Completed item scores must be finite in [0,1]')
            if answer == 'not_applicable' or (answer != 'answered' and score != 0):
                raise ValueError('Missing/refused/invalid answers must receive zero')
            weighted += weight * score
        else:
            complete = False
            if score is not None or answer != 'not_applicable':
                raise ValueError('Unresolved execution is unscored, not a model answer')
        check_evidence(item, ledger, wallet, started, jobs_root, completed=execution == 'completed')
    if not selected: raise ValueError('No selected items')
    for name, value in raw.get('capabilities', {}).items():
        if not isinstance(name, str) or not finite(value): raise ValueError('Invalid capability score')
    score = weighted/total if manifest['aggregation']['kind'] == 'weighted_mean' else raw.get('score')
    if complete and (not finite(score) or not 0 <= score <= 1):
        raise ValueError('Aggregate score must be finite in [0,1]')
    if complete and manifest['aggregation']['kind'] == 'weighted_mean' and 'score' in raw:
        if not finite(raw['score']) or not math.isclose(raw['score'], score, abs_tol=1e-8):
            raise ValueError('Reported score disagrees with frozen weights/full denominator')
    domains=domain_outputs(raw,manifest) if manifest.get('domain_aggregations') else {}
    return dict(raw, **domains, score=score if complete else None,
                score_status='valid' if complete else 'incomplete',
                execution_status='completed' if complete else 'incomplete',
                declared_items=len(declared), selected_items=selected, completed_items=attempted,
                weighted_score_lower_bound=weighted/total if manifest['aggregation']['kind'] == 'weighted_mean' else None)


def attach_accounting(state, cost):
    state['cost'] = cost
    state['accounting_status'] = 'settled' if cost['cost_complete'] else 'pending'
    return state


def public_contract(config, entry):
    from .execution_policy import policy_for
    policy = policy_for(config, entry)
    return {'protocol_version': 1, 'evaluation_policy': policy,
            **({'response_cache':{'version':1,'equivalent_test_charge':True,
                'independent_sample_header':'x-seb-sample-id',
                'default':'Identical requests reuse the same sample; specify distinct sample IDs for independent repetitions'}}
               if config.get('response_cache') and entry['wallet'] in ('development','evaluation') else {}),
            **({'joint_domains':config['joint_domains']} if config.get('joint_domains') else {}),
            'minimum_items': config.get('minimum_items', 100),
            'pilot_minimum_items': 1 if config.get('allow_pilots') and entry.get('research') else None,
            'suite_concurrency': config.get('suite_concurrency',1),
            'request_concurrency_per_model': config.get('request_concurrency_per_model'),
            'max_pending_suites': config.get('max_pending_suites'),
            'suite_cost_goal_usd': config.get('suite_cost_goal_usd',5),
            **({'item_cost_cap_usd':config['item_cost_cap_usd'],
                'suite_cost_cap_usd':entry.get('suite_cost_cap_usd',config['suite_cost_cap_usd']),
                'requires_item_id':True} if config.get('require_item_budgets') else {}),
            'suite_timeout_seconds': config.get('suite_timeout',7200),
            'research_deadline_epoch': config.get('research_deadline_epoch'),
            'output_scale': [0, 1], 'execution_statuses': ['completed', 'infra_error', 'not_run', 'budget_exhausted', 'not_selected'],
            'answer_statuses': ['answered', 'missing', 'invalid', 'refused', 'not_applicable'],
            'client_timeout_seconds': (policy['infra_retries']+1)*policy['attempt_timeout_seconds'] + 600,
            'required_submission': ['run.py', 'README.md', 'evaluation.json']}
