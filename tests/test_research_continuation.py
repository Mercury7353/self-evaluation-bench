import json
import os
from pathlib import Path
import time

import pytest

from seb.ledger import Ledger
from seb.research_continuation import prepare_continuation


def paused(tmp_path):
    out = tmp_path/'run'; out.mkdir()
    cfg = {'_config_sha256': 'frozen', 'design': {'seconds': 3600, 'rounds': 1},
           'budgets': {'development_usd': 100, 'researcher_usd': 200, 'evaluation_usd': 360}}
    researcher = {'id': 'r', 'harness': 'codex'}
    state = {'phase': 'failed', 'researcher': 'r', 'round': 1, 'started': time.time()-30,
             'mock': False, 'error': 'InterruptedError: Interrupted; existing ledgers and job IDs are preserved'}
    (out/'state.json').write_text(json.dumps(state))
    (out/'provenance.json').write_text(json.dumps({'config_sha256': 'frozen'}))
    trace = out/'researcher-trace-1'; trace.mkdir()
    (trace/'codex.process.json').write_text(json.dumps({'pid': 2147483647, 'returncode': -15,
        'finished': time.time(), 'termination_reason': 'supervisor_error'}))
    (trace/'thread.json').write_text(json.dumps({'thread_id': 'original-thread'}))
    sessions = out/'researcher-work/.codex/sessions'; sessions.mkdir(parents=True)
    (sessions/'rollout-original-thread.jsonl').write_text('saved original SDK context\n')
    (out/'researcher-rootfs').mkdir()
    ledger = Ledger(out/'gateway/ledger.sqlite')
    for wallet, cap in [('designer', 200), ('development', 100), ('evaluation', 360)]:
        ledger.wallet(wallet, cap)
    ledger.reserve('paid-original', 'designer', 'r', 1)
    ledger.finish('paid-original', .1, {}, 'completed')
    return out, cfg, researcher, ledger


def test_same_context_continuation_preserves_calls_and_is_not_repeatable(tmp_path):
    out, cfg, r, ledger = paused(tmp_path)
    before = ledger.status()
    record = prepare_continuation(out, cfg, r)
    assert record['thread_id'] == 'original-thread' and record['prior_model_calls'] == 1
    assert record['original_deadline_epoch'] == record['original_started']+3600
    assert ledger.status() == before
    assert Ledger(Path(record['directory'])/'ledger.before.sqlite').status() == before
    with pytest.raises(ValueError, match='already been attempted'):
        prepare_continuation(out, cfg, r)


@pytest.mark.parametrize('mutation', ['policy_stop', 'accounting_guard', 'unknown', 'expired',
    'configuration', 'finished', 'live', 'unfinished_job', 'outcome', 'no_context', 'other_failure', 'acceptance'])
def test_continuation_rejects_non_infrastructure_or_unreconciled_research(tmp_path, mutation):
    out, cfg, r, ledger = paused(tmp_path)
    if mutation == 'policy_stop': (out/'gateway/native-upstream-blocked.json').write_text('{}')
    elif mutation == 'accounting_guard':
        with ledger.connect() as db: db.execute("UPDATE wallets SET cap=0 WHERE name='designer'")
    elif mutation == 'unknown': ledger.reserve('unknown', 'development', 'm', 1)
    elif mutation == 'expired': cfg['design']['seconds'] = 1
    elif mutation == 'configuration': cfg['_config_sha256'] = 'changed'
    elif mutation == 'finished': (out/'freeze.json').write_text('{}')
    elif mutation == 'live':
        path = out/'researcher-trace-1/codex.process.json'; p = json.loads(path.read_text())
        p['pid'] = os.getpid(); path.write_text(json.dumps(p))
    elif mutation == 'unfinished_job':
        path = out/'research-jobs/old'; path.mkdir(parents=True); (path/'request.json').write_text('{}')
    elif mutation == 'outcome':
        path = out/'research-checkpoints'; path.mkdir(); (path/'outcome.json').write_text('{}')
    elif mutation == 'no_context': (out/'researcher-work/.codex/sessions/rollout-original-thread.jsonl').unlink()
    elif mutation == 'acceptance':
        ledger.reserve('heldout', 'evaluation', 'm', 1); ledger.finish('heldout', .1, {}, 'completed')
    else:
        path = out/'state.json'; state = json.loads(path.read_text()); state['error'] = 'RuntimeError: model failed'
        path.write_text(json.dumps(state))
    before = ledger.status()
    with pytest.raises(ValueError): prepare_continuation(out, cfg, r)
    assert ledger.status() == before and not (out/'continuations').exists()


def frozen_transfer(tmp_path, monkeypatch, *, terminal_incomplete=False):
    import seb.research_continuation as continuation
    out, cfg, r, ledger = paused(tmp_path)
    if terminal_incomplete:
        job=out/'research-jobs/retained-incomplete'; job.mkdir(parents=True)
        (job/'request.json').write_text('{}')
        (job/'result.json').write_text(json.dumps({'status':'incomplete','finished':time.time(),
            'accounting_status':'settled','cost':{'cost_complete':True,'pending_jobs':[]}}))
    monkeypatch.setattr(continuation.socket, 'gethostname', lambda: 'source-host')
    original = continuation.subprocess.check_output
    monkeypatch.setattr(continuation.subprocess, 'check_output',
        lambda command, **kwargs: 'failed\n' if command[0] == 'systemctl' else original(command, **kwargs))
    handoff = continuation.migration_handoff(out, 'destination-host', 'source.service')
    monkeypatch.setattr(continuation.socket, 'gethostname', lambda: 'destination-host')
    return out, cfg, r, ledger, handoff


def test_migration_preserves_context_without_reusing_another_hosts_pid(tmp_path, monkeypatch):
    import seb.research_continuation as continuation
    out, cfg, r, ledger, handoff = frozen_transfer(tmp_path, monkeypatch)
    before = ledger.status()
    monkeypatch.setattr(continuation.os, 'kill', lambda *_: pytest.fail('Do not probe a foreign host PID'))
    record = prepare_continuation(out, cfg, r, handoff=handoff)
    assert record['thread_id'] == 'original-thread'
    assert record['trace_directory'] == 'researcher-trace-migration-1'
    assert record['directory'] == str(out/'migrations/1')
    assert record['migration']['source_host'] == 'source-host'
    assert ledger.status() == before
    with pytest.raises(ValueError, match='already been attempted'):
        prepare_continuation(out, cfg, r, handoff=handoff)


def test_migration_keeps_terminal_incomplete_measurement_without_rerunning(tmp_path, monkeypatch):
    out, cfg, r, ledger, handoff = frozen_transfer(tmp_path, monkeypatch, terminal_incomplete=True)
    path=out/'research-jobs/retained-incomplete/result.json'; before=path.read_bytes()
    record=prepare_continuation(out, cfg, r, handoff=handoff)
    assert path.read_bytes()==before and 'retained-incomplete' in record['candidate_jobs_before']
    assert ledger.status('designer')[0]['calls']==1


@pytest.mark.parametrize('mutation', ['wrong_host', 'state', 'ledger', 'context', 'thread', 'missing_evidence'])
def test_migration_rejects_changed_source_and_wrong_destination(tmp_path, monkeypatch, mutation):
    import seb.research_continuation as continuation
    out, cfg, r, ledger, handoff = frozen_transfer(tmp_path, monkeypatch)
    if mutation == 'wrong_host': monkeypatch.setattr(continuation.socket, 'gethostname', lambda: 'another-host')
    elif mutation == 'state':
        path=out/'state.json'; path.write_text(path.read_text()+'\n')
    elif mutation == 'ledger': ledger.reserve('new-unsettled-call', 'development', 'm', 1)
    elif mutation == 'context': (out/'researcher-work/.codex/sessions/rollout-original-thread.jsonl').write_text('changed')
    else:
        record=json.loads(handoff.read_text())
        if mutation == 'thread': record['thread_id']='another-thread'
        else: del record['files']['gateway/ledger.sqlite']
        handoff.write_text(json.dumps(record))
    before=ledger.status()
    with pytest.raises(ValueError): prepare_continuation(out, cfg, r, handoff=handoff)
    assert ledger.status()==before and not (out/'migrations').exists()


@pytest.mark.parametrize('migrate', [False, True])
def test_controller_continues_original_jobs_snapshot_and_clock_through_acceptance(tmp_path, monkeypatch, migrate):
    import yaml
    import seb.experiment as experiment
    from test_experiment import fixture_config
    root = os.environ.get('SEB_TEST_ROOT'); science = os.environ.get('SEB_TEST_SCIENCE')
    if not root or not science: pytest.skip('Sandbox paths required')
    path, cfg = fixture_config(tmp_path)
    cfg['runtime'] = {'rootfs': root, 'science_packages': science}
    cfg['design']['rounds'] = 1
    cfg['evaluation']['preflight'] = False
    cfg['researchers'][0].update(harness='codex', model='gpt-6-astra', effort='xhigh',
        native_limits={'max_output_tokens': 100, 'max_context_tokens': 1000})
    for model in cfg['models']: model['model'] = model['id']
    for backend in cfg['providers'].values(): backend['upstream'] = 'http://127.0.0.1:9/v1'
    path.write_text(yaml.safe_dump(cfg)); out = tmp_path/'actual-run'
    provider = experiment.mock_provider(); build = experiment.build_gateway; contexts = []; lives = []
    def gateway(*args, **kwargs):
        kwargs['mock_url'] = f'http://127.0.0.1:{provider.server_port}'
        result = build(*args, **kwargs); contexts.append(result); return result
    lifecycle = experiment.ResearchLifecycle
    def capture(*args, **kwargs):
        life = lifecycle(*args, **kwargs); lives.append(life); return life
    monkeypatch.setattr(experiment, 'build_gateway', gateway)
    monkeypatch.setattr(experiment, 'ResearchLifecycle', capture)
    previous_jobs = []
    def native(root, work, trace, socket, token, model, prompt, **kwargs):
        trace.mkdir(); (trace/'thread.json').write_text(json.dumps({'thread_id': 'original-thread'}))
        config, tokens = contexts[-1]
        if kwargs['resume_thread'] is None:
            experiment.mock_submission(work)
            (work/'research-notes.txt').write_text('keep my research')
            sessions = work/'.codex/sessions'; sessions.mkdir(parents=True)
            (sessions/'rollout-original-thread.jsonl').write_text('saved context\n')
            lives[-1].capture(force=True)
            rows = experiment.run_jobs(config, tokens['development'], ['dev-a','dev-b','dev-c'],
                'submission', out/'already-tested', config['research_deadline_epoch'])
            assert all(row['score_status'] == 'valid' for row in rows)
            previous_jobs.extend(row['job_id'] for row in rows)
            (trace/'codex.process.json').write_text(json.dumps({'pid': 2147483647, 'returncode': -15,
                'finished': time.time(), 'termination_reason': 'supervisor_error'}))
            raise InterruptedError('Interrupted; existing ledgers and job IDs are preserved')
        assert kwargs['resume_thread'] == 'original-thread'
        assert (work/'research-notes.txt').read_text() == 'keep my research'
        assert len(lives[-1].entries) == 1
        assert contexts[0][1]['development'] != tokens['development']
        for job in previous_jobs:
            assert experiment.request(socket, tokens['development'], '/research/jobs/'+job)['status'] == 'ok'
        (trace/'codex.stdout').write_text(json.dumps({'type': 'turn.completed'})+'\n')
        return 0
    monkeypatch.setattr(experiment, 'launch_codex', native)
    try:
        with pytest.raises(InterruptedError): experiment.run(path, None, out)
        before = json.loads((out/'state.json').read_text())
        original_provenance = (out/'provenance.json').read_bytes()
        original_wallet = Ledger(out/'gateway/ledger.sqlite').status('development')
        handoff = None
        if migrate:
            import seb.research_continuation as continuation
            monkeypatch.setattr(continuation.socket, 'gethostname', lambda: 'source-host')
            original = continuation.subprocess.check_output
            monkeypatch.setattr(continuation.subprocess, 'check_output',
                lambda command, **kwargs: 'failed\n' if command[0] == 'systemctl' else original(command, **kwargs))
            handoff = continuation.migration_handoff(out, 'destination-host', 'source.service')
            monkeypatch.setattr(continuation.socket, 'gethostname', lambda: 'destination-host')
        result = experiment.run(path, None, out, resume_research=True, migration_handoff=handoff)
        after = json.loads((out/'state.json').read_text())
        assert before['started'] == after['started']
        assert contexts[-1][0]['research_deadline_epoch'] == before['started']+cfg['design']['seconds']
        assert (out/'provenance.json').read_bytes() == original_provenance
        assert Ledger(out/'gateway/ledger.sqlite').status('development') == original_wallet
        reused = json.loads((out/'development-1/results.json').read_text())
        assert set(r['job_id'] for r in reused) == set(previous_jobs)
        assert result['complete_models'] == result['expected_models']
        assert len(lives[-1].entries) == 1
        if migrate: assert (out/'researcher-trace-migration-1/thread.json').exists()
    finally:
        provider.shutdown(); provider.server_close()
