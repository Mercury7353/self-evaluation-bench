"""Explicitly exploratory public-aggregate references; never a formal panel audit."""
import json
import math

import numpy as np
from scipy.stats import pearsonr, spearmanr, kendalltau


def rank_values(values):
    """Keep floating-point summation noise from breaking mathematically tied ranks."""
    return np.round(values, decimals=12)


def validate(panel):
    if panel.get('mode')!='exploratory_aggregate' or not panel.get('source_sha256'):
        raise ValueError('An explicit, sourced exploratory reference is required')
    models=panel['models'];ids=[m['id'] for m in models]
    if len(ids)<3 or len(ids)!=len(set(ids)):raise ValueError('Invalid frozen model panel')
    if not set(panel['development_models'])<set(ids):raise ValueError('Require development and withheld models')
    if len(panel['evaluation_order'])!=len(ids) or set(panel['evaluation_order'])!=set(ids):
        raise ValueError('Evaluation order must contain the whole frozen panel exactly once')
    if not panel.get('targets'):raise ValueError('Missing references')
    for target in panel['targets']:
        seen=set()
        if not target.get('caveat'):raise ValueError('Historical protocol caveat must be explicit')
        for row in target['rows']:
            if row['model'] not in ids or row['model'] in seen:raise ValueError('Invalid reference model')
            if type(row['score']) not in (int,float) or not math.isfinite(row['score']) or not 0<=row['score']<=1:
                raise ValueError('Invalid reference score')
            seen.add(row['model'])
    return panel


def statistics(panel, rows):
    validate(panel)
    by={r['model']:r for r in rows};models={m['id']:m for m in panel['models']}
    result={'evidence_status':'exploratory_heterogeneous_public_aggregates','targets':{},
            'missing_models':[m for m in models if m not in by or by[m].get('score_status')!='valid']}
    for target in panel['targets']:
        refs={r['model']:r['score'] for r in target['rows']};groups={}
        for group,ids in [('all',list(models)),('development',panel['development_models']),
                          ('withheld',[m for m in models if m not in panel['development_models']])]:
            pairs=[{'model':m,'family':models[m]['family'],'reference':refs[m],'score':by[m]['score']}
                   for m in ids if m in refs and m in by and by[m].get('score_status')=='valid']
            x=[p['reference'] for p in pairs];y=[p['score'] for p in pairs]
            xr,yr=rank_values(x),rank_values(y)
            usable=len(pairs)>=3 and np.ptp(xr)>0 and np.ptp(yr)>0
            groups[group]={'n':len(pairs),'pairs':pairs,'expected_models':ids,
                'unmatched_or_incomplete':[m for m in ids if m not in [p['model'] for p in pairs]],
                'pearson':float(pearsonr(x,y).statistic) if usable else None,
                'spearman':float(spearmanr(xr,yr).statistic) if usable else None,
                'kendall_tau_b':float(kendalltau(xr,yr).statistic) if usable else None,
                'rank_rounding_decimal_places':12,
                'status':'exploratory' if usable else 'insufficient_or_constant_scores'}
        result['targets'][target['id']]={'caveat':target['caveat'],'groups':groups}
    return result
