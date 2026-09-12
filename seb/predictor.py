"""Isolated evaluation of a submitted predictor on held-out model families."""
import json
from pathlib import Path
import shutil
from .container import BASE_ENV,run_logged
from .cli import write
from .multitarget_runtime import summarize,measurement,training

def isolated(config, submission, payload, folder, *, panel=False):
    """Frozen code sees fold training labels and anonymous test measurements, never test labels or the host."""
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    write(folder/'input.json',payload)
    runtime=Path(__file__).with_name('multitarget_runtime.py')
    cmd=['bwrap','--ro-bind',config['base_root'],'/','--unshare-user','--uid','0','--gid','0',
        '--unshare-pid','--unshare-ipc','--unshare-uts','--unshare-net','--die-with-parent','--new-session',
        '--proc','/proc','--dev','/dev','--tmpfs','/tmp',
        '--ro-bind',str(submission),'/tmp/submission','--ro-bind',str(folder/'input.json'),'/tmp/input.json',
        '--bind',str(folder),'/tmp/results','--ro-bind',str(runtime),'/tmp/evaluate.py',
        '--ro-bind',config['science_packages'],'/tmp/science',
        '--setenv','PYTHONPATH','/tmp/science:/tmp/submission','--chdir','/tmp/submission',
        '/usr/local/bin/python','/tmp/evaluate.py','--submission','/tmp/submission',
        '--panel' if panel else '--payload','/tmp/input.json','--output','/tmp/results/output.json']
    rc=run_logged(cmd,folder/'evaluation',timeout=600,env=BASE_ENV)
    if rc:raise RuntimeError('Isolated predictor failed; see '+str(folder/'evaluation.stderr'))
    return json.loads((folder/'output.json').read_text())

def model_row(model, family, result, references):
    raw=result.get('result',{})
    scores={i['id']:i['score'] if i.get('execution_status')=='completed' else None for i in raw.get('items',[])}
    return {'id':model,'family':family,'scores':scores,'targets':references,
            'complete_source_coverage':result.get('score_status')=='valid'}


def panel_for(rows, targets, manifest):
    ids=[i['id'] for i in manifest['items']]
    return {'models':rows,'targets':targets,'task_ids':ids,
            'cost_weights':{i:1/len(ids) for i in ids},'max_cost_fraction':1.000001}


def fitted_cv(config, source, rows, targets, folder):
    """Submitted predictor runs with numeric observations only in a networkless namespace."""
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((Path(source)/'evaluation.json').read_text())
    submission=folder/'predictor';submission.mkdir()
    # Preserve optional predictor dependencies while excluding raw candidate outputs/context.
    for p in Path(source).iterdir():
        if p.is_symlink():raise ValueError('Predictor symlinks are unavailable')
        if p.is_file() and p.name=='predictor.py':shutil.copy2(p,submission/p.name)
        elif p.is_dir() and p.name=='predictor_assets':shutil.copytree(p,submission/p.name)
    if not (submission/'predictor.py').exists():
        shutil.copy2(Path(__file__).with_name('multitarget_baseline.py'),submission/'predictor.py')
    panel=panel_for([r for r in rows if r['complete_source_coverage']],targets,manifest)
    write(submission/'selection.json',panel['task_ids'])
    output=[]
    for index,family in enumerate(sorted({r['family'] for r in panel['models']})):
        train=[r for r in panel['models'] if r['family']!=family]
        test=[r for r in panel['models'] if r['family']==family]
        payload={'training':training(train,panel['task_ids']),
                 'observations':[measurement(r,panel['task_ids']) for r in test], 'targets':targets}
        preds=isolated(config,submission,payload,folder/f'fold-{index}')
        if len(preds)!=len(test):raise ValueError('Prediction count mismatch')
        output.extend({'id':r['id'],'family':family,'targets':r['targets'],'predictions':p} for r,p in zip(test,preds))
    return summarize(output,targets)|{'rows':output,'note':'Family-disjoint folds; each predictor process receives only training labels and anonymous test measurements.'}
