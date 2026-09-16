"""Target correlations and family-disjoint tests of a frozen measurement program."""
import json
from pathlib import Path
import shutil
from .cli import write
from .multitarget_runtime import summarize, training, measurement
from .overall import macro_spearman
from .predictor import fitted_cv, isolated, model_row


def with_constants(summary):
    for target,row in summary['targets'].items():
        valid=[r for r in summary['rows'] if target in r['targets'] and 'score' in r['predictions'].get(target,{})]
        x=[r['predictions'][target]['score'] for r in valid]
        y=[r['targets'][target] for r in valid]
        row['constant_prediction']=bool(len(x)>=3 and len(set(x))==1 and len(set(y))>1)
    return summary


def score_panel(config, source, results, models, references, targets, output, *, overall=None, heldout=True):
    if config.get('score_mode')=='raw_domain':
        from .raw_scoring import score_raw_panel
        return score_raw_panel(source,results,models,references,targets,output,minimum_models=(overall or {}).get('minimum_models',3))
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    by_id={r['model']:r for r in results}
    rows=[model_row(m['id'],m['family'],by_id.get(m['id'],{}),
                    {t:refs[m['id']] for t,refs in references.items() if m['id'] in refs}) for m in models]
    complete=[r for r in rows if r['complete_source_coverage']]
    raw_rows=[dict(r,predictions={t:{'score':by_id[r['id']]['result']['score']} for t in targets}) for r in complete]
    raw=with_constants(summarize(raw_rows,targets)|{'rows':raw_rows})
    errors={}
    try:cv=with_constants(fitted_cv(config,source,rows,targets,output/'family-cv'))
    except Exception as e:
        errors['family_cv']=str(e)
        cv={'targets':{},'rows':[],'error':str(e)}
    report={'raw_mean':raw,'family_cv':cv,'models':len(models),'complete_models':len(complete),'errors':errors}
    if heldout:
        dev_ids={m['id'] for m in models if m.get('split')=='development'}
        test_ids={m['id'] for m in models if m.get('split')=='holdout'}
        train=[r for r in complete if r['id'] in dev_ids];test=[r for r in complete if r['id'] in test_ids]
        try:
            submission=output/'family-cv/predictor'
            tasks=[i['id'] for i in json.loads((Path(source)/'evaluation.json').read_text())['items']]
            payload={'training':training(train,tasks),'observations':[measurement(r,tasks) for r in test],'targets':targets}
            preds=isolated(config,submission,payload,output/'new-families') if train and test else []
            evaluated=[dict(r,predictions=p) for r,p in zip(test,preds)]
            report['new_model_families']=with_constants(summarize(evaluated,targets)|{'rows':evaluated,
                'expected_models':len(test_ids),'complete_models':len(test),'training_models':len(train),
                'note':'Only development model labels train this predictor; holdout labels are never passed to it.'})
        except Exception as e:report['new_model_families']={'error':str(e)}
    if overall:
        ids={m['id'] for m in models}
        expected={t:len(ids & set(references[t])) for t in targets}
        report['overall']=macro_spearman(report[overall['source']]['targets'],expected,
            minimum_models=overall['minimum_models'],constant_prediction=overall['constant_prediction'])
        report['overall']['source']=overall['source']
    write(output/'scores.json',report)
    return report
