"""Held-out acceptance for visible prediction and sealed zero-label transfer.

Only visible development labels enter submitted code. Sealed references are
joined by the trusted scorer after predictions; no sealed fitting/CV is called.
"""
import json
import math
from pathlib import Path
import shutil
import statistics

from .cli import write
from .multitarget_runtime import corr, ranks, training, measurement
from .predictor import isolated, model_row
from .runner import digest_tree


def finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def component(predictions, references, expected, families, *, minimum_models, minimum_families, panel=None, pending_models=None):
    ids = [m for m in expected if m in references] if panel is None else list(panel)
    if len(ids)!=len(set(ids)) or any(m not in expected for m in ids):
        raise ValueError('Reference panel must contain unique expected held-out models')
    missing=[m for m in ids if m not in references or not finite(references[m])]
    base = {'expected_models': ids, 'n_reference': len(ids)-len(missing),
            'families': len({families[m] for m in ids})}
    if missing:
        return base | {'status': 'PENDING_REFERENCE', 'utility': None, 'spearman': None,
                       'missing_reference_models':missing}
    if len(ids) < minimum_models or base['families'] < minimum_families:
        return base | {'status': 'PENDING_REFERENCE', 'utility': None, 'spearman': None}
    if not all(finite(references[m]) for m in ids):
        return base | {'status': 'PENDING_REFERENCE', 'utility': None, 'spearman': None}
    y = [references[m] for m in ids]
    if len(set(y)) < 2:
        return base | {'status': 'CONSTANT_REFERENCE', 'utility': None, 'spearman': None}
    valid = [m for m in ids if finite(predictions.get(m))]
    base['n_predicted'] = len(valid)
    if len(valid) != len(ids):
        absent=[m for m in ids if m not in valid]
        pending=[m for m in absent if m in (pending_models or {})]
        if len(pending)==len(absent):
            return base | {'status':'PENDING_PROVIDER','utility':None,'spearman':None,
                           'pending_provider_models':{m:pending_models[m] for m in pending}}
        return base | {'status': 'FAIL', 'utility': -1., 'spearman': None,
                       'missing_models': [m for m in absent if m not in pending],
                       'pending_provider_models':{m:pending_models[m] for m in pending}}
    x = [predictions[m] for m in ids]
    if len(set(x)) < 2:
        return base | {'status': 'CONST', 'utility': 0., 'spearman': None, 'pearson': None}
    rho = corr(ranks(x), ranks(y))
    return base | {'status': 'OK', 'utility': rho, 'spearman': rho, 'pearson': corr(x, y)}


def summarize_outputs(models, references, visible_ids, sealed_ids, predictions, domain_scores,
                      *, minimum_models=8, minimum_families=4, reference_panels=None):
    held = [m for m in models if m['split'] == 'holdout']
    ids = [m['id'] for m in held]
    families = {m['id']: m['family'] for m in held}
    pending_models={m['id']:m['pending_reason'] for m in held if m.get('availability')=='pending'}
    if set(visible_ids) & set(sealed_ids):
        raise ValueError('Visible and sealed targets overlap')
    report = {'acceptance_models': ids, 'visible': {}, 'sealed': {}, 'fit_uses_sealed_labels': False}
    for visibility, targets in [('visible', visible_ids), ('sealed', sealed_ids)]:
        for target in targets:
            values = ({m: predictions.get(m, {}).get(target) for m in ids}
                      if visibility == 'visible' else domain_scores)
            report[visibility][target] = component(values, references.get(target, {}), ids, families,
                minimum_models=minimum_models, minimum_families=minimum_families,
                panel=reference_panels[target] if reference_panels is not None else None,pending_models=pending_models)
        components = [r['utility'] for r in report[visibility].values()]
        report[visibility + '_utility'] = statistics.mean(components) if components and all(x is not None for x in components) else None
    return report


def summarize_joint(models, references, domains, predictions, domain_scores,
                    *, minimum_models=8, minimum_families=4, reference_panels=None):
    """Score the same measurements by target, then weight each domain equally."""
    reports={domain:summarize_outputs(models,references,targets['visible'],targets['sealed'],
        predictions,domain_scores.get(domain,{}),minimum_models=minimum_models,minimum_families=minimum_families,
        reference_panels=reference_panels)
        for domain,targets in domains.items()}
    result={'domains':reports,'acceptance_models':[m['id'] for m in models if m['split']=='holdout'],
            'fit_uses_sealed_labels':False,'research_unit':'joint'}
    for visibility in ('visible','sealed'):
        result[visibility]={t:row for report in reports.values() for t,row in report[visibility].items()}
        values=[report[visibility+'_utility'] for report in reports.values()]
        result[visibility+'_utility']=statistics.mean(values) if values and all(v is not None for v in values) else None
    return result


def score_domain(config, source, development_results, acceptance_results, models, references,
                 visible_targets, sealed_targets, output, *, minimum_models=8, minimum_families=4,
                 domains=None, reference_panels=None):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    source = Path(source)
    tasks = [i['id'] for i in json.loads((source/'evaluation.json').read_text())['items']]
    dev = [m for m in models if m['split'] == 'development']
    held = [m for m in models if m['split'] == 'holdout']
    by_dev = {r['model']: r for r in development_results}
    by_test = {r['model']: r for r in acceptance_results}
    train = [model_row(m['id'], m['family'], by_dev.get(m['id'], {}),
        {t: references[t][m['id']] for t in visible_targets if m['id'] in references.get(t, {})}) for m in dev]
    train = [r for r in train if r['complete_source_coverage']]
    test = [model_row(m['id'], m['family'], by_test.get(m['id'], {}), {}) for m in held]
    test = [r for r in test if r['complete_source_coverage']]
    predictions = {}; error = None
    try:
        if not (source/'predictor.py').is_file():
            raise ValueError('Domain submission must include its visible-target predictor; no automatic replacement')
        if not train:
            raise ValueError('No complete final-snapshot development observations')
        submitted = output/'visible-predictor'; submitted.mkdir()
        shutil.copy2(source/'predictor.py', submitted/'predictor.py')
        if (source/'predictor_assets').is_dir():
            shutil.copytree(source/'predictor_assets', submitted/'predictor_assets')
        # No sealed IDs, labels, real candidate IDs or families enter the payload.
        payload = {'training': training(train, tasks), 'observations': [measurement(r, tasks) for r in test],
                   'targets': visible_targets}
        raw = isolated(config, submitted, payload, output/'visible-prediction')
        if len(raw) != len(test):
            raise ValueError('Wrong prediction count')
        predictions = {r['id']: {t: p[t]['score'] for t in visible_targets if 'score' in p.get(t, {})}
                       for r, p in zip(test, raw)}
    except Exception as exc:
        error = type(exc).__name__ + ': ' + str(exc)
    if domains is None:
        domain_scores = {m['id']: by_test[m['id']]['result'].get('score') for m in held
                         if by_test.get(m['id'], {}).get('score_status') == 'valid'}
        report = summarize_outputs(models, references, visible_targets, sealed_targets, predictions, domain_scores,
            minimum_models=minimum_models, minimum_families=minimum_families,reference_panels=reference_panels)
    else:
        domain_scores={d:{m['id']:by_test[m['id']]['result']['domain_scores'][d] for m in held
            if by_test.get(m['id'],{}).get('result',{}).get('domain_score_status',{}).get(d)=='valid'} for d in domains}
        report=summarize_joint(models,references,domains,predictions,domain_scores,
            minimum_models=minimum_models,minimum_families=minimum_families,reference_panels=reference_panels)
    # Trusted scoring input, never mounted into researcher/predictor execution.
    # Preserve predictions so adding reference labels never needs another fit or model call.
    write(output/'reference-scoring-input.json',{'version':1,'models':models,'references':references,
        'visible_targets':visible_targets,'sealed_targets':sealed_targets,'domains':domains,
        'reference_panels':reference_panels if reference_panels is not None else {
            t:[m['id'] for m in models if m['split']=='holdout' and m['id'] in references.get(t,{})]
            for t in list(visible_targets)+list(sealed_targets)},
        'predictions':predictions,'domain_scores':domain_scores,
        'minimum_models':minimum_models,'minimum_families':minimum_families,
        'submission_sha256':digest_tree(source)})
    report.update(predictions=predictions, domain_scores=domain_scores,
                  predictor_error=error, development_models=len(train),
                  complete_models=len(test), expected_models=len(held))
    write(output/'scores.json', report)
    return report


def reference_status(report):
    pending=[t for v in ('visible','sealed') for t,row in report[v].items() if row['status']=='PENDING_REFERENCE']
    failures=[t for v in ('visible','sealed') for t,row in report[v].items() if row['status']=='FAIL']
    return {'reference_status':'pending' if pending else 'ready','pending_reference_targets':pending,
            'failed_target_outputs':failures,
            'has_submission_failure':bool(failures or report.get('predictor_error'))}
