"""Versioned candidate execution policy; legacy runs opt in explicitly."""
import asyncio
import hashlib
import json
import math
import random
import re
import time
import uuid
from pathlib import Path

import httpx
from fastapi.responses import JSONResponse, Response

from .ledger import BudgetExceeded


DEFAULT_POLICY = {
    'version': 1, 'min_output_tokens': 32768, 'max_output_tokens': 131072,
    'default_output_tokens': 32768, 'infra_retries': 3,
    'attempt_timeout_seconds': 900, 'retry_backoff_seconds': 1,
    'retry_max_backoff_seconds': 30,
}


def policy_for(config, entry=None):
    if 'evaluation_policy' not in config or (entry and entry.get('wallet') == 'designer'):
        return None
    policy = DEFAULT_POLICY | config['evaluation_policy']
    if set(policy) != set(DEFAULT_POLICY) or policy['version'] != 1:
        raise ValueError('Unknown evaluation policy fields/version')
    for key in ('min_output_tokens', 'max_output_tokens', 'default_output_tokens', 'infra_retries'):
        if type(policy[key]) is not int:
            raise ValueError(key + ' must be an integer')
    if not 32768 <= policy['min_output_tokens'] <= policy['default_output_tokens'] <= policy['max_output_tokens'] <= 131072:
        raise ValueError('Candidate output limits must satisfy 32768 <= min <= default <= max <= 131072')
    if not 3 <= policy['infra_retries'] <= 10:
        raise ValueError('infra_retries must be 3..10')
    for key in ('attempt_timeout_seconds', 'retry_backoff_seconds', 'retry_max_backoff_seconds'):
        value = policy[key]
        if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
            raise ValueError(key + ' must be finite and nonnegative')
    if policy['attempt_timeout_seconds'] <= 0:
        raise ValueError('attempt_timeout_seconds must be positive')
    return policy


def output_limit(config, value=None, entry=None):
    policy = policy_for(config, entry)
    if not policy:
        value = config.get('candidate_output_tokens', 16384) if value is None else value
        low, high = 1, 16384
    else:
        value = policy['default_output_tokens'] if value is None else value
        low, high = policy['min_output_tokens'], policy['max_output_tokens']
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'Candidate output limit must be an integer in {low}..{high}; no silent clamp')
    return value


def classify_failure(status, raw=b'', *, exception=None, incomplete_stream=False):
    """Only positively identified transient infrastructure failures are retried."""
    if exception is not None:
        transient = isinstance(exception, (TimeoutError, httpx.TimeoutException, httpx.NetworkError,
                                            httpx.RemoteProtocolError))
        return ('infra_error' if transient else 'internal_error'), transient
    try:
        body = json.loads(raw)
        error = body.get('error') if isinstance(body, dict) else None
    except (ValueError, TypeError):
        error = None
    error_text = json.dumps(error).lower() if error else ''
    permanent = ('insufficient_quota', 'quota_exceeded', 'spend_limit', 'budget_exceeded',
                 'credit balance', 'insufficient balance', 'billing_hard_limit',
                 'invalid_api_key', 'authentication_error', 'permission_error')
    if any(word in error_text for word in permanent):
        return 'configuration_or_quota_error', False
    if status in (408, 429, 500, 502, 503, 504, 529):
        return 'infra_error', True
    if status is not None and status >= 400:
        return 'request_error', False
    if error:
        transient = any(word in error_text for word in ('overloaded_error', 'rate_limit_error', 'server_error'))
        return ('infra_error' if transient else 'provider_error'), transient
    if incomplete_stream:
        return 'infra_error', True
    return 'completed', False


def mask_model(raw, streaming, alias):
    """Mask API metadata only; model-generated self-identification is not erased."""
    def masked(data):
        if isinstance(data, dict):
            if 'model' in data: data['model'] = alias
            if isinstance(data.get('message'), dict) and 'model' in data['message']:
                data['message']['model'] = alias
        return data
    if not streaming:
        try: return json.dumps(masked(json.loads(raw)), ensure_ascii=False).encode()
        except ValueError: return raw
    lines = []
    for line in raw.splitlines(keepends=True):
        if line.startswith(b'data:') and line[5:].strip() != b'[DONE]':
            try: line = b'data: ' + json.dumps(masked(json.loads(line[5:])), ensure_ascii=False).encode() + b'\n'
            except ValueError: pass
        lines.append(line)
    return b''.join(lines)


async def reserve_with_backpressure(ledger, call_id, wallet, model, amount, wait_seconds=0, *, scopes=None):
    """Wait for active reservations to settle without another provider attempt.

    Unknown historical costs are never considered releasable. The atomic ledger
    remains authoritative; a genuinely insufficient wallet fails immediately.
    """
    until = time.monotonic() + wait_seconds
    while True:
        try:
            ledger.reserve(call_id, wallet, model, amount, **({'scopes':scopes} if scopes else {}))
            return
        except BudgetExceeded as error:
            with ledger.connect() as db:
                if getattr(error, 'scope', None):
                    cap = scopes[error.scope]
                    used, active = db.execute('''SELECT COALESCE(SUM(COALESCE(c.charged,c.reserve)),0),
                        COALESCE(SUM(CASE WHEN c.state='reserved' AND c.charged IS NULL THEN c.reserve ELSE 0 END),0)
                        FROM calls c JOIN call_budget_scopes s ON c.id=s.call_id WHERE s.scope=?''', (error.scope,)).fetchone()
                else:
                    cap = db.execute('SELECT cap FROM wallets WHERE name=?', (wallet,)).fetchone()['cap']
                    used, active = db.execute('''SELECT COALESCE(SUM(COALESCE(charged,reserve)),0),
                        COALESCE(SUM(CASE WHEN state='reserved' AND charged IS NULL THEN reserve ELSE 0 END),0)
                        FROM calls WHERE wallet=?''', (wallet,)).fetchone()
            if active <= 0 or cap is None or cap - (used-active) < amount - 1e-9 or time.monotonic() >= until:
                raise
            await asyncio.sleep(.2)


async def metered_request(request, body, raw_request, config, entry, ledger, *, amount, price, backend, key):
    """Buffer each attempt before release, so partial streams never concatenate retries.

    Each attempt gets its own durable charge/reservation. A completed operation can
    be replayed with the same id; an interrupted operation is never silently rerun.
    """
    from .gateway import cost, usage_from_wire
    policy = policy_for(config, entry)
    root = Path(config['artifacts'])
    scopes = dict(entry.get('budget_scopes', {}))
    item_id = request.headers.get('x-seb-item-id')
    if entry.get('item_scope_prefix'):
        if item_id not in entry.get('allowed_item_ids', []):
            return JSONResponse({'error':'A declared x-seb-item-id is required'},400)
        scopes[entry['item_scope_prefix']+hashlib.sha256(item_id.encode()).hexdigest()] = config['item_cost_cap_usd']
    operation_id = request.headers.get('x-seb-operation-id', uuid.uuid4().hex)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', operation_id):
        return JSONResponse({'error': 'Invalid operation ID'}, 400)
    fingerprint = hashlib.sha256(json.dumps({'path':request.url.path,'body':body,'policy':policy,
        'remote_model':backend.get('model',body['model']),
        'upstream':backend.get('upstream',config.get('upstream','http://127.0.0.1:9')),
        **({'budget_scopes':scopes,'item_id':item_id} if scopes else {})},
        sort_keys=True,separators=(',',':')).encode()).hexdigest()
    scope_key = hashlib.sha256((entry['wallet'] + '\0' + operation_id).encode()).hexdigest()
    operation = root / 'operations' / scope_key
    operation.parent.mkdir(exist_ok=True)
    try:
        operation.mkdir()
    except FileExistsError:
        try: previous = json.loads((operation/'result.json').read_text())
        except (FileNotFoundError, ValueError):
            return JSONResponse({'error': 'operation_incomplete', 'operation_id': operation_id,
                                 'message': 'Reconcile the existing operation; do not submit another ID'}, 409)
        if previous['fingerprint'] != fingerprint:
            return JSONResponse({'error': 'Operation ID reused with different input'}, 409)
        return Response((operation/'response.body').read_bytes(), status_code=previous['http_status'],
                        media_type=previous['media_type'], headers=previous['headers'] | {'x-seb-replayed': 'true'})
    (operation/'request.json').write_text(json.dumps({'operation_id': operation_id, 'fingerprint': fingerprint,
                                                    'wallet': entry['wallet'], 'created': time.time()}))
    upstream = backend.get('upstream', config.get('upstream', 'http://127.0.0.1:9')).rstrip('/')
    upstream_body = dict(body, model=backend.get('model', body['model']))
    upstream_raw = json.dumps(upstream_body, ensure_ascii=False).encode()
    headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
               'anthropic-version': request.headers.get('anthropic-version', '2023-06-01')}
    attempts = []
    final_raw, final_status, media_type = b'', 500, 'application/json'
    for index in range(policy['infra_retries'] + 1):
        if time.time()>=entry.get('deadline_epoch',float('inf')):
            return JSONResponse({'error':{'type':'deadline_exceeded','message':'No new attempts after the execution deadline'}},409)
        call_id = uuid.uuid4().hex
        folder = root/'wire'/call_id; folder.mkdir(parents=True)
        (folder/'request.body').write_bytes(raw_request)
        (folder/'upstream.request.body').write_bytes(upstream_raw)
        meta = {'id': call_id, 'operation_id': operation_id, 'attempt': index + 1,
                'wallet': entry['wallet'], 'model': body['model'], 'path': request.url.path,
                'created': time.time(), 'reservation_usd': amount, 'policy': policy,
                'request_sha256': hashlib.sha256(raw_request).hexdigest(),
                'upstream_request_sha256': hashlib.sha256(upstream_raw).hexdigest()}
        for scope in entry.get('trace_scopes', []):
            if not re.fullmatch('[0-9a-f]{32}', scope): raise ValueError('Invalid trace scope')
            marker = root/'scopes'/scope; marker.mkdir(parents=True, exist_ok=True)
            (marker/call_id).touch()
        try:
            meta.update(state='waiting_for_reservation')
            (folder/'meta.json').write_text(json.dumps(meta, indent=2))
            await reserve_with_backpressure(ledger, call_id, entry['wallet'], body['model'], amount,
                                            config.get('reservation_wait_seconds', 0), scopes=scopes)
            meta.pop('state')
        except BudgetExceeded:
            meta.update(state='rejected_budget', finished=time.time())
            (folder/'meta.json').write_text(json.dumps(meta, indent=2))
            attempts.append({'id': call_id, 'state': 'rejected_budget'})
            final_status = 402
            final_raw = json.dumps({'error': {'type': 'budget_exceeded', 'message': 'Insufficient reservation budget'},
                                    'operation_id': operation_id}).encode()
            media_type = 'application/json'
            break
        (folder/'meta.json').write_text(json.dumps(meta, indent=2))
        raw, status, streaming, terminal, charge, retry, response_headers = b'', None, False, False, None, False, {}
        try:
            async with asyncio.timeout(policy['attempt_timeout_seconds']):
                async with httpx.AsyncClient(timeout=httpx.Timeout(policy['attempt_timeout_seconds'], connect=30), trust_env=False) as client:
                    async with client.stream('POST', upstream + request.url.path, content=upstream_raw, headers=headers) as response:
                        status = response.status_code
                        response_headers = dict(response.headers)
                        streaming = 'text/event-stream' in response.headers.get('content-type', '')
                        with (folder/'response.body').open('wb') as out:
                            async for chunk in response.aiter_bytes():
                                out.write(chunk); out.flush()
            raw = (folder/'response.body').read_bytes()
            usage, terminal = usage_from_wire(raw, streaming)
            state, retry = classify_failure(status, raw, incomplete_stream=streaming and not terminal)
            if state == 'completed' and not streaming:
                try:
                    data = json.loads(raw)
                    if not isinstance(data, dict) or not any(k in data for k in ('content', 'choices', 'input_tokens')):
                        raise ValueError('Missing provider response envelope')
                except (ValueError, TypeError):
                    state, retry = 'invalid_provider_response', False
            # Terminal streaming error events are provider failures, not model answers.
            if streaming:
                for line in raw.splitlines():
                    if not line.startswith(b'data:'): continue
                    try: event = json.loads(line[5:])
                    except ValueError: continue
                    if isinstance(event, dict) and event.get('error'):
                        state, retry = classify_failure(status, line[5:]); break
            if terminal: charge = cost(usage, price)
            if request.url.path.endswith('/count_tokens') and state == 'completed': charge = 0.
        except asyncio.CancelledError:
            ledger.finish(call_id, None, {}, 'transport_unknown')
            meta.update(state='transport_unknown', complete=False, finished=time.time())
            (folder/'meta.json').write_text(json.dumps(meta, indent=2))
            raise
        except Exception as error:
            raw = (folder/'response.body').read_bytes() if (folder/'response.body').exists() else b''
            usage, _ = usage_from_wire(raw, streaming)
            state, retry = classify_failure(status, raw, exception=error)
            meta['error'] = type(error).__name__
        if charge is not None and charge > amount + 1e-9:
            state, retry = 'reservation_bound_exceeded', False
            with ledger.connect() as db: db.execute('UPDATE wallets SET cap=0 WHERE name=?', (entry['wallet'],))
        ledger_state = 'completed' if state == 'completed' and terminal else state
        ledger.finish(call_id, charge, usage, ledger_state)
        meta.update(state=ledger_state, complete=state == 'completed' and terminal, http_status=status,
                    retryable=retry, charge_usd=charge, usage=usage, finished=time.time(),
                    response_sha256=hashlib.sha256(raw).hexdigest(), response_bytes=len(raw))
        (folder/'meta.json').write_text(json.dumps(meta, indent=2))
        attempts.append({'id': call_id, 'state': ledger_state, 'retryable': retry})
        if state == 'completed':
            final_raw = mask_model(raw, streaming, body['model']) if backend.get('model') else raw
            final_status, media_type = status, ('text/event-stream' if streaming else 'application/json')
            break
        # Do not expose provider error messages (which may contain model IDs/URLs).
        final_raw = json.dumps({'type': 'error', 'error': {'type': state,
                               'message': 'Candidate request failed; inspect attempt evidence'},
                               'operation_id': operation_id}).encode()
        # 422 prevents clients/harnesses adding another automatic 5xx retry loop.
        final_status, media_type = 422, 'application/json'
        if not retry or index == policy['infra_retries']: break
        delay = min(policy['retry_max_backoff_seconds'], policy['retry_backoff_seconds'] * 2 ** index)
        try: delay = max(delay, min(float(response_headers.get('retry-after', 0)), policy['retry_max_backoff_seconds']))
        except ValueError: pass
        await asyncio.sleep(delay + random.uniform(0, delay * .1))
    response_meta = {'fingerprint': fingerprint, 'http_status': final_status, 'media_type': media_type,
                     'attempts': attempts, 'headers': {'x-seb-request-id': attempts[-1]['id'],
                     'x-seb-operation-id': operation_id, 'x-seb-attempt-count': str(len(attempts)),
                     'x-seb-retry-managed': 'true'}}
    (operation/'response.body').write_bytes(final_raw)
    temporary = operation/'result.tmp'; temporary.write_text(json.dumps(response_meta, indent=2))
    temporary.replace(operation/'result.json')
    return Response(final_raw, status_code=final_status, media_type=media_type, headers=response_meta['headers'])
