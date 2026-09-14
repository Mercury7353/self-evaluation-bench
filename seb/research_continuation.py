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
import socket
import sqlite3
import subprocess
import time

from .runner import digest_tree


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _assert_dead(record):
    if 'returncode' not in record or 'finished' not in record:
        raise ValueError('Source process termination is not recorded')
    try:
        os.kill(record['pid'], 0)
    except ProcessLookupError:
        return
    raise ValueError('Source process is still present')


def migration_handoff(output, destination_host, source_service):
    """Freeze a host transfer on the stopped source, outside researcher mounts.

    This is an explicit host move, separate from the one local continuation.
    The destination rechecks these hashes before opening the original wallet.
    """
    output = Path(output).resolve()
    host = socket.gethostname()
    if not destination_host or destination_host == host:
        raise ValueError('Migration requires a different, explicit destination host')
    status = subprocess.check_output(['systemctl', '--user', 'show', source_service,
                                     '-p', 'ActiveState', '--value'], text=True).strip()
    if status not in ('inactive', 'failed'):
        raise ValueError('Stop the source service before freezing its migration')
    state = json.loads((output/'state.json').read_text())
    if state.get('phase') != 'failed' or not state.get('error', '').startswith('InterruptedError:'):
        raise ValueError('Only an operator-interrupted source may migrate')
    trace_name = ('researcher-trace-continuation-1' if state.get('research_continuation')
                  else 'researcher-trace-1')
    if (output/'migrations').exists():
        raise ValueError('A host migration has already been attempted')
    trace = output/trace_name
    for path in [trace/'codex.process.json', *output.glob('research-jobs/*/execution/*.process.json')]:
        _assert_dead(json.loads(path.read_text()))
    thread = json.loads((trace/'thread.json').read_text())['thread_id']
    rollouts = list((output/'researcher-work/.codex/sessions').rglob('*'+thread+'*.jsonl'))
    if len(rollouts) != 1:
        raise ValueError('Original SDK context is missing or ambiguous')
    paths = [output/'state.json', output/'provenance.json', output/'gateway/ledger.sqlite',
             trace/'codex.process.json', trace/'thread.json', rollouts[0]]
    paths.extend(output.glob('research-jobs/*/request.json'))
    paths.extend(output.glob('research-jobs/*/result.json'))
    record = {'version': 1, 'kind': 'host_migration', 'output': str(output),
              'source_host': host, 'destination_host': destination_host,
              'source_service': source_service, 'source_service_state': status,
              'source_trace': trace_name, 'thread_id': thread,
              'frozen_at': time.time(),
              'files': {str(p.relative_to(output)): _sha(p) for p in paths}}
    path = output/'migration-handoff.json'
    with path.open('x') as stream:
        stream.write(json.dumps(record, indent=2))
    path.chmod(0o600)
    return path


def prepare_continuation(output, cfg, researcher, *, handoff=None):
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
    migration = None
    if handoff:
        if Path(handoff).resolve() != (output/'migration-handoff.json').resolve():
            raise ValueError('Use the controller-owned migration handoff')
        migration = json.loads(Path(handoff).read_text())
        if (migration.get('kind') != 'host_migration' or migration.get('version') != 1
                or migration.get('output') != str(output.resolve())
                or migration.get('source_service_state') not in ('inactive', 'failed')
                or migration.get('source_host') == socket.gethostname()
                or migration.get('destination_host') != socket.gethostname()
                or migration.get('source_trace') not in ('researcher-trace-1', 'researcher-trace-continuation-1')):
            raise ValueError('Migration must run on the verified destination after source shutdown')
        required = {'state.json', 'provenance.json', 'gateway/ledger.sqlite',
                    migration['source_trace']+'/codex.process.json', migration['source_trace']+'/thread.json'}
        if not required.issubset(migration.get('files', {})):
            raise ValueError('Migration handoff is missing source evidence')
        for name, digest in migration['files'].items():
            path = (output/name).resolve()
            if not path.is_relative_to(output.resolve()) or _sha(path) != digest:
                raise ValueError('Source evidence changed after migration handoff: '+name)
    destination = output/('migrations/1' if migration else 'continuations/1')
    if destination.parent.exists():
        raise ValueError('A research continuation or host migration has already been attempted')
    trace = output/(migration['source_trace'] if migration else 'researcher-trace-1')
    process = json.loads((trace/'codex.process.json').read_text())
    if ('returncode' not in process or 'finished' not in process
            or process.get('termination_reason') != 'supervisor_error'):
        raise ValueError('Original native harness termination is not recorded')
    # PIDs are host-local. The source verified termination before freezing the
    # handoff; checking that PID on another host could target an unrelated user.
    if not migration:
        _assert_dead(process)
    thread = json.loads((trace/'thread.json').read_text())['thread_id']
    if migration and thread != migration['thread_id']:
        raise ValueError('Migration cannot change the SDK thread')
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
        status = json.loads(result.read_text()) if result.exists() else {}
        terminal_incomplete = (migration and status.get('status') == 'incomplete'
            and status.get('finished') and status.get('accounting_status') == 'settled'
            and status.get('cost', {}).get('cost_complete') is True
            and not status.get('cost', {}).get('pending_jobs'))
        if status.get('status') not in ('ok', 'error') and not terminal_incomplete:
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
              'trace_directory': 'researcher-trace-migration-1' if migration else 'researcher-trace-continuation-1',
              'host': socket.gethostname(), 'migration': migration,
              'reason': 'Operator infrastructure interruption; same SDK thread, clock, workspace and ledger'}
    (destination/'record.json').write_text(json.dumps(record, indent=2))
    return record
