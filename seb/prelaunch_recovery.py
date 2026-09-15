"""Recover only a failed launch with zero provider calls, preserving its clock.

This is not a researcher retry facility. Any ledger call, including a failed or
unsettled one, makes the run ineligible. The caller must stop its supervisor first.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import time


def archive_unstarted(output, cfg, researcher_id):
    output=Path(output)
    state=json.loads((output/'state.json').read_text())
    provenance=json.loads((output/'provenance.json').read_text())
    if state.get('phase')!='failed' or state.get('researcher')!=researcher_id:
        raise ValueError('Only a failed launch of the same researcher can be recovered')
    if provenance['config_sha256']!=cfg['_config_sha256']:
        raise ValueError('Prelaunch recovery cannot change the frozen configuration')
    deadline=state['started']+cfg['design']['seconds']
    if deadline<=time.time():raise ValueError('Original research deadline has expired')
    ledger=output/'gateway/ledger.sqlite'
    with sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro',uri=True) as db:
        if db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]:
            raise ValueError('Any existing model call forbids automatic prelaunch recovery')
        wallets=dict(db.execute('SELECT name,cap FROM wallets').fetchall())
        for name,key in [('designer','researcher_usd'),('development','development_usd'),('evaluation','evaluation_usd')]:
            if wallets.get(name)!=cfg['budgets'][key]:
                raise ValueError('Preserve the original wallet caps; no reset or restoration')
    # Require recorded harness termination as well as the controller's failed state.
    for process in output.glob('researcher-trace-*/*.process.json'):
        record=json.loads(process.read_text())
        if 'returncode' not in record:
            raise ValueError('Harness termination has not been recorded')
    parent=output.parent/'prelaunch-attempts'/output.name
    if parent.exists() and any(parent.iterdir()):
        raise ValueError('Only one automatic prelaunch recovery is permitted')
    parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    archive=parent/('attempt-'+str(len(list(parent.iterdir()))+1))
    output.rename(archive)
    return {'archive':str(archive),'original_started':state['started'],
            'original_deadline_epoch':deadline,'prior_model_calls':0,
            'prior_ledger_sha256':hashlib.sha256((archive/'gateway/ledger.sqlite').read_bytes()).hexdigest(),
            'recovered_at':time.time(),'reason':'Infrastructure repair before first model request; no new research allocation'}


def restore_ledger(recovery, output):
    original=Path(recovery['archive'])/'gateway/ledger.sqlite'
    destination=Path(output)/'gateway/ledger.sqlite'
    destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():raise ValueError('Never overwrite a recovered ledger')
    with sqlite3.connect(original.resolve().as_uri()+'?mode=ro',uri=True) as source:
        with sqlite3.connect(destination) as target:source.backup(target)


def archive_verified_preflight(output, cfg, researcher_id):
    """Continue a failed transport-only preflight after validating saved responses.

    No response is regenerated or rescored and no uncertain charge is released.
    Only the initial preflight is eligible, before any researcher invocation.
    """
    output=Path(output)
    state=json.loads((output/'state.json').read_text())
    if state.get('phase')!='failed' or state.get('researcher')!=researcher_id or state.get('error')!='RuntimeError: Model transport preflight incomplete; no researcher launched':
        raise ValueError('Not a failed transport-only preflight')
    if list(output.glob('researcher-trace-*')) or (output/'researcher-work').exists():
        raise ValueError('Researcher must not have started')
    if json.loads((output/'provenance.json').read_text())['config_sha256']!=cfg['_config_sha256']:
        raise ValueError('Preflight recovery cannot change configuration')
    deadline=state['started']+cfg['design']['seconds']
    if deadline<=time.time():raise ValueError('Original deadline expired')
    ledger=output/'gateway/ledger.sqlite'
    with sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro',uri=True) as db:
        rows=db.execute('SELECT id,wallet,model,state FROM calls').fetchall()
        if not rows or any(wallet!='development' for _,wallet,_,_ in rows):raise ValueError('Only preflight calls allowed')
        candidates={m['id'] for m in cfg['models']}
        if {m for _,_,m,_ in rows}!=candidates:raise ValueError('Incomplete candidate preflight coverage')
        for ident,_,model,status in rows:
            if status=='completed':continue
            if status!='response_translation_error':raise ValueError('Unresolved nontranslation failure')
            folder=output/'gateway/wire'/ident;meta=json.loads((folder/'meta.json').read_text());raw=(folder/'response.body').read_bytes()
            if meta['http_status']!=200 or hashlib.sha256(raw).hexdigest()!=meta['response_sha256']:raise ValueError('Invalid saved response')
            from .chat_candidate import CandidateAdapter
            import tempfile
            response=json.loads(raw)
            with tempfile.TemporaryDirectory() as temp:
                adapter=CandidateAdapter(temp,{'model':response.get('model'),'effort':'max','allow_unreconciled_usage':True},model,'offline')
                adapter.translate(response,False)
        wallets=dict(db.execute('SELECT name,cap FROM wallets').fetchall())
        for name,key in [('designer','researcher_usd'),('development','development_usd'),('evaluation','evaluation_usd')]:
            if wallets.get(name)!=cfg['budgets'][key]:raise ValueError('Wallet cap changed')
    parent=output.parent/'preflight-attempts'/output.name
    if parent.exists():raise ValueError('One transport preflight continuation only')
    parent.mkdir(parents=True);archive=parent/'attempt-1';output.rename(archive)
    return {'archive':str(archive),'original_started':state['started'],'original_deadline_epoch':deadline,
            'prior_model_calls':len(rows),'preflight_offline_verified':True,'recovered_at':time.time(),
            'reason':'Saved HTTP200 responses verified offline; raw results, usage, ledger rows and unknown reservations preserved; no repeated calls'}
