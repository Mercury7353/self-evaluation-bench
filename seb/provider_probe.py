"""Metered transport probes; fixed operation IDs prevent accidental repeat calls.

Use a dedicated existing allocation. No researcher or target task is launched.
Reports omit credentials, response text and upstream error bodies.
"""
import asyncio
import fcntl
import os
import json
from pathlib import Path
import time

import httpx

from .gateway import create_app


def persist(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False))
    os.replace(temporary, path)


async def probe(config, token, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with (output / "probe.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return await _probe(config, token, output)


async def _probe(config, token, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    entry = config['tokens'][token]
    state_path = output / 'result.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        'started': time.time(), 'purpose': 'transport_only', 'rows': []}
    completed = {r['model'] for r in state['rows']}
    if state.get('stopped_on_quota'):
        return state
    app = create_app(config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://probe', timeout=None) as client:
        for model in entry['models']:
            if model in completed:
                continue
            operation = 'transport-' + model
            body = {'model': model, 'max_tokens': 32768,
                    'messages': [{'role': 'user', 'content': 'Reply with exactly READY.'}]}
            response = await client.post('/anthropic/v1/messages', json=body,
                headers={'x-api-key': token, 'x-seb-operation-id': operation})
            row = {'model': model, 'http_status': response.status_code,
                   'operation_id': operation, 'requested_max_tokens': 32768,
                   'request_id': response.headers.get('x-seb-request-id')}
            try:
                data = response.json()
                text = ''.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text')
                row.update(nonempty_answer=bool(text.strip()), usage=data.get('usage'),
                           stop_reason=data.get('stop_reason'))
                if isinstance(data.get('error'), dict):
                    row['error_type'] = data['error'].get('type')
            except (ValueError, AttributeError, TypeError):
                row['nonempty_answer'] = False
            row['transport_ok'] = response.status_code == 200
            state['rows'].append(row)
            if response.status_code == 402 or row.get('error_type') in ('configuration_or_quota_error', 'policy_error'):
                state['stopped_on_quota'] = True
                state['stop_reason'] = row.get('error_type', 'budget_exceeded')
            persist(state_path, state)
            if state.get('stopped_on_quota'):
                break
    state['finished'] = time.time()
    state['all_transport_ok'] = len(state['rows']) == len(entry['models']) and all(r['transport_ok'] for r in state['rows'])
    persist(state_path, state)
    return state


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if len(config['tokens']) != 1:
        raise ValueError('Exactly one scoped probe allocation required')
    result = asyncio.run(probe(config, next(iter(config['tokens'])), args.output))
    print(json.dumps({'models_checked': len(result['rows']), 'all_transport_ok': result['all_transport_ok'],
                      'stopped_on_quota': result.get('stopped_on_quota', False)}))
