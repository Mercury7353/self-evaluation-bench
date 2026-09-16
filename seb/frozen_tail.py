"""Prepare missing-only execution of a frozen suite without changing its functions."""
import json
import shutil
from pathlib import Path


def prepare(source, destination, previous, model):
    source,destination=Path(source),Path(destination)
    manifest=json.loads((source/'evaluation.json').read_text())
    assets=json.loads((source/'questions.json').read_text())
    declared={i['id'] for i in manifest['items']}
    old={i['id']:i for i in previous.get('items',[])}
    if not set(old)<=declared:raise ValueError('Previous items differ from frozen manifest')
    kept={k:v for k,v in old.items() if v.get('execution_status')=='completed'}
    # This adapter only supplements operations with no candidate evidence. Unknown
    # attempts require offline reconciliation first, never automatic resampling.
    for k,row in old.items():
        if k not in kept and (row.get('evidence') or row.get('operation_id') or row.get('answer_operation_id')):
            raise ValueError('Unreconciled existing operation: '+k)
    missing=declared-set(kept)
    for scenario in assets['scenarios']:
        ids={c[0] for c in scenario['criteria']}
        if ids&missing and ids&set(kept):raise ValueError('Partially graded scenario needs response replay')
    shutil.copytree(source,destination)
    (destination/'frozen_program.py').write_bytes((source/'run.py').read_bytes())
    (destination/'selection.json').write_text(json.dumps({'model':model,'missing':sorted(missing),'retained':sorted(kept)},indent=2))
    manifest['items']=[i for i in manifest['items'] if i['id'] in missing]
    for agg in manifest.get('domain_aggregations',{}).values():
        if 'weights' in agg:
            agg['weights']={k:v for k,v in agg['weights'].items() if k in missing}
            # Partial wrapper scores are discarded; only original per-item grades are merged.
            if not agg['weights'] and missing:agg['weights']={sorted(missing)[0]:1.0}
    (destination/'evaluation.json').write_text(json.dumps(manifest,indent=2))
    (destination/'run.py').write_text('''import argparse,json,concurrent.futures
from pathlib import Path
import frozen_program as frozen
from research_sdk import Client
p=argparse.ArgumentParser();p.add_argument('--context');p.add_argument('--output');a=p.parse_args()
root=Path(__file__).parent
selection=json.loads((root/'selection.json').read_text());missing=set(selection['missing'])
assets=json.loads((root/'questions.json').read_text());client=Client(a.context)
assert client.context['model']==selection['model']
rows={};out=Path(a.output)
def save():
 frozen.atomic_json(out,{'protocol_version':1,'items':[rows[k] for k in selection['missing'] if k in rows]})
save()
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
 jobs=[pool.submit(frozen.evaluate_direct,client,q) for q in assets['direct_items'] if q['id'] in missing]
 for job in concurrent.futures.as_completed(jobs):
  row=job.result();rows[row['id']]=row;save();print(row['id'],row['execution_status'],flush=True)
for scenario in assets['scenarios']:
 ids={c[0] for c in scenario['criteria']}
 if ids<=missing:
  for row in frozen.evaluate_scenario(client,scenario):rows[row['id']]=row
  save()
assert set(rows)==missing
''')
    return {'model':model,'retained':kept,'missing':sorted(missing),'original_item_count':len(declared)}


def merge(original, supplemental, manifest):
    rows={i['id']:i for i in original.get('items',[]) if i.get('execution_status')=='completed'}
    ids=[i['id'] for i in manifest['items']]
    for row in supplemental.get('items',[]):
        if row['id'] in rows:raise ValueError('Refuse to overwrite completed answer')
        if row['id'] not in ids:raise ValueError('Unknown supplemental item')
        rows[row['id']]=row
    return {'protocol_version':1,'items':[rows.get(k,{'id':k,'score':None,'execution_status':'not_run','answer_status':'not_applicable','evidence':[]}) for k in ids]}
