"""White-box feedback scoped to one development wallet and frozen submission."""
import hashlib
import json
from pathlib import Path
from .cli import write
from .scoring import score_panel


def feedback(config, entry, jobs, identifiers):
    if not entry.get('research') or not config.get('whitebox'):
        raise ValueError('White-box feedback is available only to the development researcher')
    if not isinstance(identifiers,list) or not identifiers or len(set(identifiers))!=len(identifiers):
        raise ValueError('Provide unique completed suite job IDs')
    selected=[];sources=set();models=set()
    for ident in identifiers:
        if not isinstance(ident,str) or len(ident)!=32 or any(c not in '0123456789abcdef' for c in ident):raise ValueError('Invalid job ID')
        meta=json.loads((jobs/ident/'request.json').read_text());result=json.loads((jobs/ident/'result.json').read_text())
        if meta['wallet']!=entry['wallet'] or meta['model'] not in entry['models'] or meta['kind']!='suite':raise ValueError('Job outside this development wallet')
        if meta['model'] in models:raise ValueError('One job per model; no best-of-answer selection')
        if result['status'] in ('queued','running'):raise ValueError('Wait for the same job ID to finish')
        selected.append(dict(result,model=meta['model']));sources.add(meta['submission_id']);models.add(meta['model'])
    if len(sources)!=1:raise ValueError('Feedback requires the same immutable suite across models')
    if len(models)<3:raise ValueError('At least three distinct development models required')
    key=hashlib.sha256(json.dumps(sorted(identifiers)).encode()).hexdigest()
    folder=jobs.parent/'feedback'/entry['wallet']/key
    cached=folder/'scores.json'
    if cached.exists():return json.loads(cached.read_text())
    wb=config['whitebox']
    return score_panel(config,jobs/identifiers[0]/'submitted',selected,
        [m for m in wb['models'] if m['id'] in models],wb['references'],wb['targets'],folder,
        overall=config['overall'],heldout=False)
