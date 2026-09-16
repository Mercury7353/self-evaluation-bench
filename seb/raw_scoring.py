"""Correlate frozen measured domain scores directly, without fitting a predictor."""
import json
import statistics
import itertools
from pathlib import Path
from .cli import write
from .evaluation import domain_outputs
from .domain_scoring import component


def measured_panel(source, results):
    manifest=json.loads((Path(source)/'evaluation.json').read_text())
    scores={d:{} for d in manifest.get('domain_aggregations',{})}
    for result in results:
        if result.get('score_status')!='valid':continue
        outputs=domain_outputs(result['result'],manifest)
        for d,value in outputs['domain_scores'].items():
            if outputs['domain_score_status'][d]=='valid':scores[d][result['model']]=value
    return scores


def score_raw_panel(source, results, models, references, targets, output, *, minimum_models=3):
    scores=measured_panel(source,results);ids=[m['id'] for m in models]
    families={m['id']:m['family'] for m in models};rows={};domains={}
    for target,metadata in targets.items():
        domain=metadata['domain'];values=scores.get(domain,{})
        row=component(values,references.get(target,{}),ids,families,
            minimum_models=minimum_models,minimum_families=1)
        row.update(domain=domain,score_source='measured_domain',distinct_scores=len(set(values.values())))
        available=[m for m in ids if m in values and m in references.get(target,{})]
        pairs={'concordant':0,'discordant':0,'measured_tie':0,'reference_tie':0}
        for a,b in itertools.combinations(available,2):
            dx=values[a]-values[b];dy=references[target][a]-references[target][b]
            key='reference_tie' if dy==0 else 'measured_tie' if dx==0 else 'concordant' if dx*dy>0 else 'discordant'
            pairs[key]+=1
        row['pairwise']=pairs
        row['measured_score_range']=[min(values.values()),max(values.values())] if values else None
        rows[target]=row;domains.setdefault(domain,[]).append(row['utility'])
    domain_utilities={d:statistics.mean(v) if v and all(x is not None for x in v) else None for d,v in domains.items()}
    utilities=list(domain_utilities.values())
    report={'measured_domain_scores':scores,'targets':rows,'domain_utilities':domain_utilities,
            'models':len(ids),'complete_models':sum(all(m in v for v in scores.values()) for m in ids) if scores else 0,
            'overall':{'metric':'domain_macro_spearman','source':'raw_domain','score':statistics.mean(utilities) if utilities and all(v is not None for v in utilities) else None},
            'note':'Direct frozen measured scores; no target-label fitting, prediction, calibration or clipping. Constant measured scores have zero ranking utility; correlation itself is undefined.'}
    write(Path(output)/'scores.json',report);return report
