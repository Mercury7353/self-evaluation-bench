"""Native researcher Responses transport using the episode's bounded ledger.

The SDK wire body is not translated. Only the configured researcher token may
use these routes. Unknown calls remain reserved; the transport never retries.
"""
import asyncio
import hashlib
import json
import math
import re
import time
import uuid
from pathlib import Path

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .ledger import BudgetExceeded


def usage_cost(usage, price, *, cached=False):
    """Responses input_tokens includes cache reads/writes; do not add them twice."""
    details = usage.get('input_tokens_details') or {}
    total, output = usage['input_tokens'], usage['output_tokens']
    read, write = details.get('cached_tokens', 0), details.get('cache_write_tokens', 0)
    if any(type(n) is not int or n < 0 for n in (total, output, read, write)) or read + write > total:
        raise ValueError('Invalid Responses token usage')
    if cached and read and 'cache_read_multiplier' not in price:
        return None
    ordinary = total - read - write
    billed_input = ordinary + read * (price.get('cache_read_multiplier', 1) if cached else 1)
    billed_input += write * price.get('cache_write_multiplier', 2)
    long = price.get('long_context', {})
    expensive = total > long.get('threshold_tokens', float('inf'))
    return (billed_input * price['input'] * (long.get('input_multiplier', 1) if expensive else 1)
            + output * price['output'] * (long.get('output_multiplier', 1) if expensive else 1)) / 1e6


def read_usage(raw, streaming, *, compact=False):
    if not streaming:
        try:
            response = json.loads(raw)
            terminal = response.get('status') if response.get('status') in ('completed', 'incomplete', 'failed') else None
            if compact and response.get('object') == 'response.compaction':
                terminal = 'completed'
            return response.get('usage'), terminal
        except (ValueError, AttributeError):
            return None, False
    usage, terminal = None, False
    for line in raw.splitlines():
        if not line.startswith(b'data:'):
            continue
        try:
            event = json.loads(line[5:])
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') in ('response.completed', 'response.incomplete', 'response.failed'):
            usage, terminal = event.get('response', {}).get('usage'), event['type'].removeprefix('response.')
    return usage, terminal


def request_bound(body, spec, price, *, compact=False):
    from .gateway import reservation
    if not isinstance(body, dict) or body.get('model') != spec['model']:
        raise ValueError('Native researcher model is frozen')
    if not compact and body.get('reasoning', {}).get('effort') != spec['effort']:
        raise ValueError('Native researcher reasoning effort is frozen')
    for field in ('previous_response_id', 'conversation', 'prompt'):
        if body.get(field):
            raise ValueError('Remote input references have no local billing bound')
    if body.get('background') or body.get('context_management'):
        raise ValueError('Only foreground requests and explicit compaction are budgeted')
    maximum = body.get('max_output_tokens', spec['max_output_tokens'])
    if type(maximum) is not int or not 0 < maximum <= spec['max_output_tokens']:
        raise ValueError('Native output bound exceeds the frozen model limit')
    if compact and body.get('stream'):
        raise ValueError('Compaction is not a streaming endpoint')
    opaque = False
    def inspect(value):
        nonlocal opaque
        if isinstance(value, dict):
            if value.get('type') in ('input_image', 'input_file', 'input_video', 'output_audio') or value.get('file_id'):
                raise ValueError('Multimodal and remote file input require a separate cost bound')
            if value.get('encrypted_content'):
                opaque = True
            for nested in value.values():
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)
    inspect(body)
    def flatten(group):
        for tool in group:
            if tool.get('type') == 'namespace':
                yield from flatten(tool.get('tools', []))
            else:
                yield tool
    tools = list(flatten(body.get('tools', [])))
    inputs = body.get('input', [])
    for item in inputs if isinstance(inputs, list) else []:
        if isinstance(item, dict) and item.get('type') == 'additional_tools':
            tools.extend(flatten(item.get('tools', [])))
    bound = reservation({**body, 'tools': tools, 'max_tokens': maximum}, price)
    if opaque:
        # Encrypted state can expand beyond its byte length. Reserve a full model
        # context instead of guessing the token count of the opaque payload.
        full = {'input_tokens': spec['max_context_tokens'], 'output_tokens': maximum,
                'input_tokens_details': {'cache_write_tokens': spec['max_context_tokens']}}
        bound = max(bound, usage_cost(full, price))
    return bound


def install_routes(app, config, ledger, access):
    spec = config.get('native_researcher')
    if not spec:
        return
    model_id = spec['id']
    if not spec.get('model') or not spec.get('effort'):
        raise ValueError('Native model and effort must be explicit')
    for name in ('max_output_tokens', 'max_context_tokens'):
        if type(spec.get(name)) is not int or spec[name] <= 0:
            raise ValueError('Native model limits must be positive integers')
    if spec['max_output_tokens'] > 131072:
        raise ValueError('Unsupported native output billing bound')
    timeout = spec.get('timeout_seconds', 600)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Native transport timeout must be positive')
    backend = config['model_backends'][model_id]
    if backend['model'] != spec['model']:
        raise ValueError('Native researcher backend mismatch')
    price = config['prices'][model_id]
    entries = [entry for entry in config['tokens'].values() if entry.get('native_responses')]
    if not entries or any(entry['models'] != [model_id] or entry['wallet'] != 'designer'
                          or type(entry.get('cap')) not in (int, float)
                          or not math.isfinite(entry['cap']) or entry['cap'] <= 0
                          or not math.isfinite(entry.get('deadline_epoch', float('inf')))
                          for entry in entries):
        raise ValueError('Native researcher needs a bounded designer wallet and deadline')
    root = Path(config['artifacts'])
    blocked = root / 'native-upstream-blocked.json'

    @app.post('/v1/responses')
    @app.post('/v1/responses/compact')
    async def proxy(request: Request):
        entry = access(request)
        if not entry or not entry.get('native_responses'):
            return JSONResponse({'error': 'Native researcher access required'}, 403)
        if time.time() >= entry['deadline_epoch']:
            from .limit_events import deadline_response
            return deadline_response(config, entry)
        if blocked.exists():
            return JSONResponse({'error': 'Native upstream is blocked; preserve the existing run'}, 403)
        raw = await request.body()
        compact = request.url.path.endswith('/compact')
        try:
            body = json.loads(raw)
            amount = request_bound(body, spec, price, compact=compact)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            return JSONResponse({'error': str(error)}, 400)
        operation_id = request.headers.get('x-seb-operation-id', uuid.uuid4().hex)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', operation_id):
            return JSONResponse({'error': 'Invalid operation ID'}, 400)
        ident = hashlib.sha256((entry['wallet'] + '\0native\0' + operation_id).encode()).hexdigest()
        fingerprint = hashlib.sha256(request.url.path.encode() + b'\0' + raw).hexdigest()
        folder = root / 'native-wire' / ident
        folder.parent.mkdir(exist_ok=True)
        try:
            folder.mkdir()
        except FileExistsError:
            try:
                previous = json.loads((folder / 'meta.json').read_text())
            except (FileNotFoundError, ValueError):
                previous = {}
            if previous.get('fingerprint') != fingerprint or not previous.get('response_complete'):
                return JSONResponse({'error': 'Reconcile the existing native operation; do not change its ID'}, 409)
            return Response((folder / 'response.body').read_bytes(), status_code=previous['http_status'],
                            media_type=previous['media_type'], headers={'x-seb-replayed': 'true'})
        (folder / 'request.body').write_bytes(raw)
        meta = {'id': ident, 'operation_id': operation_id, 'fingerprint': fingerprint,
                'wallet': entry['wallet'], 'model': model_id, 'provider_model': spec['model'],
                'protocol': 'native-responses', 'path': request.url.path, 'created': time.time(),
                'reservation_usd': amount, 'state': 'reserved', 'response_complete': False}
        def save():
            temporary = folder / 'meta.tmp'
            temporary.write_text(json.dumps(meta, indent=2))
            temporary.replace(folder / 'meta.json')
        try:
            ledger.reserve(ident, entry['wallet'], model_id, amount, scopes=entry.get('budget_scopes'))
        except BudgetExceeded as error:
            meta.update(state='rejected_budget'); save()
            return JSONResponse({'error': str(error)}, 402)
        save()
        client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(30, timeout)), trust_env=False)
        key = Path(backend['key_file']).read_text().strip()
        address = backend['upstream'].rstrip('/') + ('/responses/compact' if compact else '/responses')
        try:
            response = await client.send(client.build_request('POST', address, content=raw,
                headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}), stream=True)
        except (Exception, asyncio.CancelledError) as error:
            ledger.finish(ident, None, {}, 'transport_unknown')
            meta.update(state='transport_unknown', error_type=type(error).__name__, finished=time.time()); save()
            await client.aclose()
            if isinstance(error, asyncio.CancelledError):
                raise
            return JSONResponse({'error': 'Native transport failed; reservation retained'}, 502)
        streaming = 'text/event-stream' in response.headers.get('content-type', '')
        media_type = 'text/event-stream' if streaming else 'application/json'
        meta.update(http_status=response.status_code, media_type=media_type,
                    provider_request_id=response.headers.get('x-request-id'))

        def finish(content, delivered):
            usage, terminal = read_usage(content, streaming, compact=compact)
            charge, estimate = None, None
            try:
                if usage and terminal:
                    charge = usage_cost(usage, price)
                    estimate = usage_cost(usage, price, cached=True)
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
            state = 'completed' if response.is_success and terminal in ('completed', 'incomplete') and charge is not None else 'error_or_unknown'
            if charge is not None and charge > amount + 1e-9:
                state = 'reservation_bound_exceeded'
                with ledger.connect() as db:
                    db.execute('UPDATE wallets SET cap=0 WHERE name=?', (entry['wallet'],))
            ledger.finish(ident, charge, usage or {}, state)
            # Persist billing/policy rejection; SDK and gateway do not restart it.
            if not response.is_success:
                try:
                    error = json.loads(content).get('error') or {}
                    code = error.get('code') or error.get('type')
                    if code in ('insufficient_quota', 'project_spend_limit_exceeded', 'cyber_policy'):
                        blocked.write_text(json.dumps({'id': ident, 'code': code, 'at': time.time()}))
                except (ValueError, AttributeError):
                    pass
            meta.update(state=state, usage=usage, charge_usd=charge, cache_adjusted_estimate_usd=estimate,
                        response_complete=delivered, terminal=bool(terminal), provider_status=terminal, finished=time.time(),
                        response_sha256=hashlib.sha256(content).hexdigest())
            save()

        async def relay():
            delivered = False
            try:
                with (folder / 'response.body').open('wb') as output:
                    async with asyncio.timeout(timeout):
                        async for chunk in response.aiter_bytes():
                            output.write(chunk); output.flush()
                            yield chunk
                delivered = True
            finally:
                content = (folder / 'response.body').read_bytes() if (folder / 'response.body').exists() else b''
                finish(content, delivered)
                await response.aclose(); await client.aclose()
        return StreamingResponse(relay(), status_code=response.status_code, media_type=media_type,
                                 headers={'x-seb-request-id': ident})
