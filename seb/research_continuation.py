"""Continue an explicitly interrupted native research context, never retry a result.

The operator must first stop the original supervisor. This deliberately narrow
path requires settled calls and terminal candidate jobs; it cannot repair policy
stops, exhausted accounting, completed research, or an expired research window.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

from .runner import digest_tree


def prepare_continuation(output, cfg, researcher):
    output = Path(output)
    state = json.loads((output/'state.json').read_text())
    provenance = json.loads((output/'provenance.json').read_text())
    if (researcher['harness'] != 'codex' or cfg['design']['rounds'] != 1
            or state.get('researcher') != researcher['id'] or state.get('phase') != 'failed'
            or state.get('round') != 1 or state.get('mock')
            or state.get('error') != 'InterruptedError: Interrupted; existing ledgers and job IDs are preserved'):
        raise ValueError('Continuation requires an operator-interrupted first-round native Codex researcher')
    if provenance['config_sha256'] != cfg['_config_sha256']:
        raise ValueError('Continuation cannot change the frozen configuration')
    deadline = state['started'] + cfg['design']['seconds']
    if deadline <= time.time():
        raise ValueError('Original research deadline has expired')
    for name in ('freeze.json', 'result.json', 'checkpoints.json', 'checkpoints',
                 'acceptance-input', 'research-checkpoints/stop.json',
                 'research-checkpoints/outcome.json', 'gateway/native-upstream-blocked.json'):
        if (output/name).exists():
            raise ValueError('Research already stopped or entered finalization: '+name)
    destination = output/'continuations/1'
    if destination.parent.exists():
        raise ValueError('A research continuation has already been attempted')
    trace = output/'researcher-trace-1'
    process = json.loads((trace/'codex.process.json').read_text())
    if ('returncode' not in process or 'finished' not in process
            or process.get('termination_reason') != 'supervisor_error'):
        raise ValueError('Original native harness termination is not recorded')
    try:
        os.kill(process['pid'], 0)
    except ProcessLookupError:
        pass
    else:
        raise ValueError('Original native harness is still present')
    thread = json.loads((trace/'thread.json').read_text())['thread_id']
    work = output/'researcher-work'
    rollouts = list((work/'.codex/sessions').rglob('*'+thread+'*.jsonl'))
    if len(rollouts) != 1 or not rollouts[0].stat().st_size or not (output/'researcher-rootfs').is_dir():
        raise ValueError('Original SDK context or isolated rootfs is missing')
    entries_path = output/'research-checkpoints/index.json'
    entries = json.loads(entries_path.read_text()) if entries_path.exists() else []
    for index, entry in enumerate(entries, 1):
        path = output/'research-checkpoints'/f'{index:06d}'
        if entry['sequence'] != index or Path(entry['path']) != path or digest_tree(path) != entry['files']:
            raise ValueError('Saved research checkpoint changed before continuation')
    jobs = {}
    for job in (output/'research-jobs').glob('*'):
        request = job/'request.json'
        if not request.exists():
            continue
        result = job/'result.json'
        if not result.exists() or json.loads(result.read_text()).get('status') not in ('ok', 'error'):
            raise ValueError('Unfinished candidate jobs require separate reconciliation')
        jobs[job.name] = {name: hashlib.sha256((job/name).read_bytes()).hexdigest()
                          for name in ('request.json', 'result.json')}
    ledger = output/'gateway/ledger.sqlite'
    with sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro', uri=True) as db:
        count = db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]
        if not count or db.execute('SELECT COUNT(*) FROM calls WHERE charged IS NULL').fetchone()[0]:
            raise ValueError('Continuation requires prior calls fully settled; unknown costs stay reserved')
        if db.execute("SELECT COUNT(*) FROM calls WHERE wallet='evaluation'").fetchone()[0]:
            raise ValueError('Independent acceptance has already started')
        wallets = dict(db.execute('SELECT name,cap FROM wallets').fetchall())
        for name, key in [('designer', 'researcher_usd'), ('development', 'development_usd'), ('evaluation', 'evaluation_usd')]:
            if wallets.get(name) != cfg['budgets'][key]:
                raise ValueError('Preserve original wallet caps, including accounting closures')
        for name, cap in wallets.items():
            used = db.execute('SELECT COALESCE(SUM(charged),0) FROM calls WHERE wallet=?', (name,)).fetchone()[0]
            if cap is not None and (cap <= 0 or used > cap + 1e-9):
                raise ValueError('Accounting violation forbids continuation')
        destination.mkdir(parents=True, mode=0o700)
        with sqlite3.connect(destination/'ledger.before.sqlite') as snapshot:
            db.backup(snapshot)
    for name in ('state.json', 'provenance.json'):
        shutil.copy2(output/name, destination/name)
    record = {'original_started': state['started'], 'original_deadline_epoch': deadline,
              'thread_id': thread, 'prior_model_calls': count, 'prior_saved_snapshots': len(entries),
              'rollout': str(rollouts[0]), 'rollout_sha256_before': hashlib.sha256(rollouts[0].read_bytes()).hexdigest(),
              'ledger_before_sha256': hashlib.sha256((destination/'ledger.before.sqlite').read_bytes()).hexdigest(),
              'candidate_jobs_before': jobs, 'directory': str(destination), 'continued_at': time.time(),
              'reason': 'Operator infrastructure interruption; same SDK thread, clock, workspace and ledger'}
    (destination/'record.json').write_text(json.dumps(record, indent=2))
    return record
