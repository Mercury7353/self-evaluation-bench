"""Run frozen, local-graded items through isolated Codex-login processes.

This is a separately labeled harness, not a transparent API replacement.
Credentials and invocation templates are supplied outside the repository.
"""
import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def parse_events(raw):
    events = []
    for line in raw.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    completed = [e for e in events if e.get('type') == 'turn.completed']
    tools = [e for e in events if e.get('type') in ('item.started', 'item.completed')
             and e.get('item', {}).get('type') not in ('agent_message', 'reasoning', 'error')]
    answers = [e['item']['text'] for e in events
               if e.get('type') == 'item.completed'
               and e.get('item', {}).get('type') == 'agent_message']
    errors = [str(e.get('message') or e.get('error') or '') for e in events
              if e.get('type') in ('error', 'turn.failed')]
    return {'terminal': bool(completed), 'tools': tools,
            'answer': answers[-1] if answers else '',
            'usage': completed[-1].get('usage') if completed else None,
            'errors': errors}


def terminate(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def grade(rootfs, suite, item_id, answer):
    program = '''import sys,json,types,importlib.util
sys.modules['research_sdk']=types.SimpleNamespace(Client=None)
s=importlib.util.spec_from_file_location('frozen','/tmp/suite/run.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
q=json.load(sys.stdin)
print(json.dumps(m.grade(q['id'],q['answer'])))
'''
    args = ['bwrap', '--ro-bind', str(rootfs), '/', '--unshare-user', '--uid', '0',
            '--gid', '0', '--unshare-pid', '--unshare-ipc', '--unshare-uts',
            '--unshare-net', '--die-with-parent', '--proc', '/proc', '--dev', '/dev',
            '--tmpfs', '/tmp', '--ro-bind', str(suite), '/tmp/suite',
            '/usr/local/bin/python', '-c', program]
    p = subprocess.run(args, input=json.dumps({'id': item_id, 'answer': answer}),
                       text=True, capture_output=True, timeout=30,
                       env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    if p.returncode:
        raise RuntimeError('isolated grader failed: ' + p.stderr[-1000:])
    return json.loads(p.stdout)


def main():
    if sys.version_info < (3, 9):
        raise SystemExit('Python 3.9+ required; validate interpreter before inference.')
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    out, suite = Path(cfg['output']), Path(cfg['suite'])
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'STOP').exists():
        raise SystemExit('User stop is active; refusing new inference.')
    lockfile = (out / 'controller.lock').open('w')
    fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    manifest = json.loads((suite / 'evaluation.json').read_text())
    assert hashlib.sha256((suite / 'evaluation.json').read_bytes()).hexdigest() == cfg['manifest_sha256']
    prompts = {q['id']: q['prompt'] for q in manifest['questions']}
    spec = json.loads((suite / 'grader_spec.json').read_text())
    template = json.loads(Path(cfg['template']).read_text())
    items = manifest['items'][:args.limit] if args.limit else manifest['items']
    mutex = threading.Lock()
    model_stop = {m['id']: threading.Event() for m in cfg['models']}
    results = {}
    started = time.time()
    deadline = started + cfg.get('controller_seconds', 21600)

    def persist():
        summary = {}
        for model in cfg['models']:
            mid = model['id']
            rows = [results[(mid, i['id'])] for i in manifest['items'] if (mid, i['id']) in results]
            completed = {r['id']: r for r in rows if r['execution_status'] == 'completed'}
            domains = {}
            for name, aggregate in manifest['domain_aggregations'].items():
                weights = aggregate['weights']
                available = {k: w for k, w in weights.items() if k in completed}
                den = sum(available.values())
                domains[name] = {'complete': len(available) == len(weights),
                                 'completed': len(available), 'expected': len(weights),
                                 'weighted_coverage': den / sum(weights.values()),
                                 'observed_score': sum(completed[k]['score'] * w for k, w in available.items()) / den if den else None}
            usage = {k: sum((r.get('usage') or {}).get(k, 0) for r in rows)
                     for k in ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')}
            cost = ((usage['input_tokens'] - usage['cached_input_tokens']) * model['price']['input']
                    + usage['cached_input_tokens'] * model['price']['input'] * model['price'].get('cache_read_multiplier', 1)
                    + usage['output_tokens'] * model['price']['output']) / 1e6
            payload = {'model': model, 'route': 'codex-chatgpt-login', 'items': rows,
                       'completed': len(completed), 'expected': len(manifest['items']),
                       'domains': domains, 'usage': usage, 'api_equivalent_estimate_usd': cost,
                       'estimate_is_invoice': False, 'api_ledger_modified': False}
            dump(out / mid / 'result.json', payload)
            summary[mid] = {k: payload[k] for k in ('completed', 'expected', 'domains', 'usage', 'api_equivalent_estimate_usd')}
        dump(out / 'state.json', {'phase': 'running', 'started': started, 'updated': time.time(),
                                 'pid': os.getpid(), 'models': summary,
                                 'scope': 'separate Codex-login completion, frozen answers and graders',
                                 'authorization': cfg['authorization']})

    def work(model, item):
        mid, iid = model['id'], item['id']
        location = out / mid / iid
        location.mkdir(parents=True, exist_ok=True)
        saved = location / 'item.json'
        if saved.exists():
            row = json.loads(saved.read_text())
            # Preserve terminal wrong/empty answers; retries only happen inside
            # this operation, never silently across controller restarts.
            with mutex:
                results[(mid, iid)] = row
                persist()
            return
        if model_stop[mid].is_set() or time.time() >= deadline or (out / 'STOP').exists():
            return
        row = {'id': iid, 'question_id': item['question_id'], 'route': 'codex-chatgpt-login',
               'execution_status': 'not_run', 'answer_status': 'not_applicable', 'score': None,
               'original_max_output_tokens': spec.get('tokens', {}).get(iid, 1536),
               'original_output_cap_enforced': False,
               'requested_model': model['model'], 'effort': model['effort'],
               'prompt_sha256': hashlib.sha256(prompts[item['question_id']].encode()).hexdigest()}
        for attempt in range(1, cfg.get('attempts', 3) + 1):
            if time.time() >= deadline or model_stop[mid].is_set() or (out / 'STOP').exists():
                break
            attempt_dir = location / f'attempt-{attempt:02d}'
            attempt_dir.mkdir(exist_ok=False)
            prefix = template['prefix'][:]
            index = prefix.index('/tmp/codex-home')
            prefix[index - 1] = model['private_home']
            # The only mounted writable directory for the response is this attempt.
            cli_index = len(prefix) - 1
            prefix[cli_index:cli_index] = ['--bind', str(attempt_dir), '/tmp/output']
            settings = template['config'][:]
            settings += ['-c', 'model=' + json.dumps(model['model']),
                         '-c', 'model_reasoning_effort=' + json.dumps(model['effort'])]
            command = prefix + ['exec', '--ignore-user-config', '--ephemeral', '--skip-git-repo-check',
                                '-C', '/tmp/workspace', '-s', 'read-only', '--json',
                                '-o', '/tmp/output/answer.txt'] + settings + ['-']
            (attempt_dir / 'prompt.txt').write_text(prompts[item['question_id']])
            t0 = time.time()
            timeout = False
            with (attempt_dir / 'events.jsonl').open('wb') as stdout, (attempt_dir / 'stderr.txt').open('wb') as stderr:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                           start_new_session=True, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
                dump(attempt_dir / 'process.json', {'pid': process.pid, 'started': t0, 'model': model['model']})
                try:
                    process.communicate(prompts[item['question_id']].encode(), timeout=min(cfg.get('attempt_seconds', 900), max(1, deadline-time.time())))
                except subprocess.TimeoutExpired:
                    timeout = True
                    terminate(process)
            parsed = parse_events((attempt_dir / 'events.jsonl').read_text())
            error = '\n'.join(parsed['errors'])
            dump(attempt_dir / 'process.json', {'pid': process.pid, 'started': t0, 'finished': time.time(),
                                               'returncode': process.returncode, 'timeout': timeout,
                                               'terminal_received': parsed['terminal'], 'model': model['model']})
            row.update(attempts=attempt, evidence=str(attempt_dir), usage=parsed['usage'])
            if parsed['tools']:
                row.update(execution_status='protocol_error', error='Unexpected tool event; excluded from scoring')
                model_stop[mid].set()
                break
            if parsed['terminal']:
                answer_path = attempt_dir / 'answer.txt'
                answer = answer_path.read_text() if answer_path.exists() else parsed['answer']
                row.update(execution_status='completed', **grade(cfg['rootfs'], suite, iid, answer))
                row['answer_sha256'] = hashlib.sha256(answer.encode()).hexdigest()
                break
            row.update(execution_status='infrastructure_error', error=error or (attempt_dir/'stderr.txt').read_text()[-2000:])
            lower = row['error'].lower()
            if any(x in lower for x in ('usage limit', 'quota', 'not supported', 'model_not_found', 'unsupported', 'unauthorized', 'authentication', 'policy', 'forbidden')):
                model_stop[mid].set()
                break
            if attempt < cfg.get('attempts', 3):
                time.sleep(min(30, 5 * attempt))
        dump(saved, row)
        with mutex:
            results[(mid, iid)] = row
            persist()
        print(mid, iid, row['execution_status'], row.get('score'), flush=True)

    persist()
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.get('concurrency', 6)) as pool:
        futures = [pool.submit(work, model, item) for item in items for model in cfg['models']]
        for future in concurrent.futures.as_completed(futures):
            future.result()
    state = json.loads((out / 'state.json').read_text())
    state.update(phase='completed' if all(r['completed'] == r['expected'] for r in state['models'].values()) else 'incomplete',
                 finished=time.time())
    dump(out / 'state.json', state)


if __name__ == '__main__':
    main()
