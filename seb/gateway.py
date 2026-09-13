"""Anthropic/OpenAI wire proxy: raw bodies saved verbatim, conservative budgets.

No upstream auth header is written to the trajectory. Stream fragments are
persisted before forwarding. A client disconnect retains the charge reservation.
"""
import asyncio
import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .ledger import BudgetExceeded, Ledger
from .execution_policy import policy_for, output_limit, metered_request


def usage_from_wire(raw, streaming):
    usage = {}
    complete = not streaming
    if not streaming:
        try:
            d = json.loads(raw)
            usage.update(d.get('usage', {}))
        except (ValueError, TypeError):
            pass
        return usage, complete
    for line in raw.decode('utf-8', errors='replace').splitlines():
        if not line.startswith('data:'):
            continue
        s = line[5:].strip()
        if s == '[DONE]':
            complete = True
            continue
        try:
            d = json.loads(s)
        except ValueError:
            continue
        usage.update(d.get('message', {}).get('usage', {}) or {})
        usage.update(d.get('usage', {}) or {})
        if d.get('type') == 'message_stop':
            complete = True
    return usage, complete


def cost(usage, price):
    if 'input_tokens_details' in usage:
        from .native_responses import usage_cost
        return usage_cost(usage, price)
    # Conservatively charge cached input at standard input, and cache creation
    # at the most expensive published write multiplier. Never double-discount.
    if 'input_tokens' in usage:
        i = usage.get('input_tokens', 0) + usage.get('cache_read_input_tokens', 0)
        context_tokens = i + usage.get('cache_creation_input_tokens', 0)
        i += usage.get('cache_creation_input_tokens', 0) * price.get('cache_write_multiplier', 2)
        o = usage.get('output_tokens', 0)
    elif 'prompt_tokens' in usage:
        i, o = usage['prompt_tokens'], usage.get('completion_tokens', 0)
        context_tokens = i
    else:
        return None
    long = price.get('long_context', {})
    expensive = context_tokens > long.get('threshold_tokens', float('inf'))
    return (i * price['input'] * (long.get('input_multiplier', 1) if expensive else 1) +
            o * price['output'] * (long.get('output_multiplier', 1) if expensive else 1)) / 1_000_000


def reservation(body, price):
    # Deliberately conservative UTF-8 byte bound + envelope; no multimodal input
    # is admitted under this text-only MVP accounting contract.
    def inspect(x):
        if isinstance(x, dict):
            if x.get('type') in ('image', 'image_url', 'input_audio', 'document', 'video'):
                raise ValueError('Multimodal requests need a separate billing bound')
            for v in x.values(): inspect(v)
        elif isinstance(x, list):
            for v in x: inspect(v)
    inspect(body)
    if body.get('n', 1) != 1 or body.get('best_of', 1) != 1:
        raise ValueError('Only one completion per request is budgeted')
    if body.get('service_tier') not in (None, 'default', 'standard'):
        raise ValueError('Only the frozen standard service tier is budgeted')
    if any(t.get('type') not in (None, 'function', 'custom') for t in body.get('tools', [])):
        raise ValueError('Provider-hosted tools have no configured cost bound')
    if 'max_tokens' in body and 'max_completion_tokens' in body and body['max_tokens'] != body['max_completion_tokens']:
        raise ValueError('Conflicting output-token limits')
    n = body.get('max_tokens', body.get('max_completion_tokens'))
    if not isinstance(n, int) or isinstance(n, bool) or not 0 < n <= 131072:
        raise ValueError('Explicit max_tokens (1..131072) is required')
    input_bound = len(json.dumps(body, ensure_ascii=False).encode()) + 4096
    output_headroom = price.get('reservation_output_headroom', 1.0)
    if (isinstance(output_headroom, bool) or not isinstance(output_headroom, (int, float))
            or not math.isfinite(output_headroom) or not 1 <= output_headroom <= 2):
        raise ValueError('Reservation output headroom must be between 1 and 2')
    # Maximum cache creation multiplier; prompt/tool framing allowance included.
    long = price.get('long_context', {})
    expensive = input_bound > long.get('threshold_tokens', float('inf'))
    return (input_bound * price['input'] * max(1, price.get('cache_write_multiplier', 2)) *
            (long.get('input_multiplier', 1) if expensive else 1) + n * output_headroom * price['output'] *
            (long.get('output_multiplier', 1) if expensive else 1)) / 1_000_000


def create_app(config):
    policy_for(config)  # Fail invalid new protocols before creating wallets.
    if config.get('response_cache'):
        from .response_cache import validate_config
        if not policy_for(config):raise ValueError('Response cache requires the metered candidate policy')
        config['response_cache']=validate_config(config['response_cache'],[
            config.get('base_root'),config.get('science_packages'),
            *[e.get('workspace') for e in config['tokens'].values()]])
    root = Path(config['artifacts'])
    root.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(root / 'ledger.sqlite')
    for entry in config['tokens'].values():
        ledger.wallet(entry['wallet'], entry['cap'])
    app = FastAPI()
    app.state.ledger = ledger
    app.state.trial_lock = asyncio.Lock()
    app.state.trial_jobs = set()
    key = Path(config['key_file']).read_text().strip()
    upstream = config.get('upstream', 'http://127.0.0.1:9').rstrip('/')

    def access(request):
        token = request.headers.get('x-api-key') or request.headers.get('authorization', '').removeprefix('Bearer ')
        return config['tokens'].get(token)

    from .native_responses import install_routes
    install_routes(app, config, ledger, access)

    @app.get('/health')
    async def health():
        return {'status': 'ok'}

    @app.get('/budget')
    async def budget(request: Request):
        entry = access(request)
        if not entry: return JSONResponse({'error': 'Unauthorized'}, 401)
        return ledger.status(entry['wallet'])

    @app.post('/anthropic/v1/messages')
    @app.post('/anthropic/v1/messages/count_tokens')
    @app.post('/v1/openai/chat/completions')
    async def proxy(request: Request):
        entry = access(request)
        if not entry: return JSONResponse({'error': 'Unauthorized'}, 401)
        if entry.get('native_responses'):
            return JSONResponse({'error': 'Use the frozen native Responses researcher route'}, 403)
        if time.time()>=entry.get('deadline_epoch',float('inf')):
            from .limit_events import deadline_response
            return deadline_response(config,entry)
        raw_request = await request.body()
        try:
            body = json.loads(raw_request)
            model = body['model']
            if model not in entry['models'] or model not in config['prices']:
                return JSONResponse({'error': 'Model not allowed for this wallet'}, 403)
            price = config['prices'][model]
            counting = request.url.path.endswith('/count_tokens')
            if config.get('require_item_budgets') and entry.get('research') and not counting:
                return JSONResponse({'error':'Run candidate measurements as a suite or pilot with declared item IDs'},403)
            if policy_for(config, entry) and not counting:
                output_limit(config, body.get('max_tokens', body.get('max_completion_tokens')), entry)
                for field,value in config.get('frozen_request_parameters',{}).get(model,{}).items():
                    if field in body and body[field]!=value:
                        raise ValueError('Request conflicts with frozen parameter: '+field)
                    body[field]=value
            amount = 0 if counting else reservation(body, price)
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({'error': str(e)}, 400)
        call_id = uuid.uuid4().hex
        if policy_for(config, entry):
            backend = config.get('model_backends', {}).get(model, {})
            request_key = Path(backend['key_file']).read_text().strip() if backend.get('key_file') else key
            async def execute():
                if time.time()>=entry.get('deadline_epoch',float('inf')):
                    from .limit_events import deadline_response
                    return deadline_response(config,entry)
                return await metered_request(request, body, raw_request, config, entry, ledger,
                                             amount=amount, price=price, backend=backend, key=request_key)
            limit=config.get('request_concurrency_per_model')
            if limit:
                if not hasattr(app.state,'model_slots'): app.state.model_slots={}
                slots=app.state.model_slots.setdefault(model,asyncio.Semaphore(limit))
                async with slots: return await execute()
            return await execute()
        folder = root / 'wire' / call_id
        folder.mkdir(parents=True)
        (folder / 'request.body').write_bytes(raw_request)
        meta = {'id': call_id, 'wallet': entry['wallet'], 'model': model,
                'path': request.url.path, 'created': time.time(), 'reservation_usd': amount,
                'request_sha256': hashlib.sha256(raw_request).hexdigest()}
        scopes=entry.get('trace_scopes',[])
        if scopes:
            meta['trace_scopes']=list(scopes)
            for scope in scopes:
                if len(scope)!=32 or any(c not in '0123456789abcdef' for c in scope):raise ValueError('Invalid internal trace scope')
                index=root/'scopes'/scope;index.mkdir(parents=True,exist_ok=True)
                (index/call_id).touch()
        backend = config.get('model_backends', {}).get(model, {})
        if backend.get('model'):
            body['model'] = backend['model']
            raw_request = json.dumps(body).encode()
        request_upstream = backend.get('upstream', upstream).rstrip('/')
        request_key = Path(backend['key_file']).read_text().strip() if backend.get('key_file') else key
        headers = {'Authorization': 'Bearer ' + request_key, 'Content-Type': 'application/json',
                   'anthropic-version': request.headers.get('anthropic-version', '2023-06-01')}
        if config.get('count_input_tokens') and not counting and request.url.path == '/anthropic/v1/messages':
            # Provider counts have underestimated actual billed input in live calls.
            # Keep the byte bound as a floor; counting may only increase it.
            count_body = {k: body[k] for k in ('model', 'messages', 'system', 'tools', 'tool_choice', 'thinking') if k in body}
            (folder / 'count.request.json').write_text(json.dumps(count_body))
            try:
                async with httpx.AsyncClient(timeout=45, trust_env=False) as counter:
                    counted = await counter.post(request_upstream + '/anthropic/v1/messages/count_tokens', json=count_body, headers=headers)
                (folder / 'count.response.body').write_bytes(counted.content)
                n_input = counted.json().get('input_tokens')
                if counted.is_success and isinstance(n_input, int) and not isinstance(n_input, bool) and n_input >= 0:
                    n_output = body.get('max_tokens', body.get('max_completion_tokens'))
                    counted_amount = ((n_input + 1024) * price['input'] * max(1, price.get('cache_write_multiplier', 2)) + n_output * price['output']) / 1_000_000
                    amount = max(amount, counted_amount)
                    meta.update(input_count=n_input, reservation_method='max_byte_bound_provider_count', reservation_usd=amount)
            except Exception as e:
                meta['count_error'] = type(e).__name__
        try:
            ledger.reserve(call_id, entry['wallet'], model, amount)
        except BudgetExceeded as e:
            meta['state'] = 'rejected_budget'
            (folder / 'meta.json').write_text(json.dumps(meta, indent=2))
            return JSONResponse({'type': 'error', 'error': {'type': 'budget_exceeded', 'message': str(e)}}, 402)
        (folder / 'meta.json').write_text(json.dumps(meta, indent=2))
        # Do not forward auth, cookies, or arbitrary beta headers from clients.
        client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30), trust_env=False)
        req = client.build_request('POST', request_upstream + request.url.path, content=raw_request, headers=headers)
        try:
            response = await client.send(req, stream=True)
        except Exception as e:
            ledger.finish(call_id, None, {}, 'transport_unknown')
            meta.update(state='transport_unknown', error=type(e).__name__)
            (folder / 'meta.json').write_text(json.dumps(meta, indent=2))
            await client.aclose()
            return JSONResponse({'error': 'Upstream transport failure', 'request_id': call_id}, 502)
        meta['http_status'] = response.status_code
        meta['response_headers'] = {k: v for k, v in response.headers.items() if k.lower() not in ('set-cookie', 'authorization')}
        is_stream = 'text/event-stream' in response.headers.get('content-type', '')

        async def relay():
            completed = False
            try:
                with (folder / 'response.body').open('wb') as f:
                    async for chunk in response.aiter_bytes():
                        f.write(chunk)
                        f.flush()
                        yield chunk
                    os.fsync(f.fileno())
                completed = True
            finally:
                await response.aclose()
                await client.aclose()
                raw = (folder / 'response.body').read_bytes()
                usage, terminal = usage_from_wire(raw, is_stream)
                charge = cost(usage, price) if completed and terminal else None
                if counting and completed and response.is_success: charge = 0.0
                # Errors without usage remain reserved: upstream charging is unknown.
                state = 'completed' if completed and terminal and response.is_success else 'incomplete_or_error'
                if charge is not None and charge > amount + 1e-9:
                    state = 'reservation_bound_exceeded'
                    # Ledger still records actual charge; halt this wallet thereafter.
                    with ledger.connect() as db:
                        db.execute('UPDATE wallets SET cap=0 WHERE name=?', (entry['wallet'],))
                ledger.finish(call_id, charge, usage, state)
                meta.update(state=state, complete=completed and terminal, usage=usage,
                            charge_usd=charge, finished=time.time(), response_bytes=len(raw),
                            response_sha256=hashlib.sha256(raw).hexdigest())
                (folder / 'meta.json').write_text(json.dumps(meta, indent=2))
        return StreamingResponse(relay(), status_code=response.status_code,
                                 media_type=response.headers.get('content-type', 'application/json'),
                                 headers={'x-seb-request-id': call_id})
    return app


def main():
    import argparse
    import uvicorn
    p = argparse.ArgumentParser()
    p.add_argument('--config', required=True)
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--uds')
    a = p.parse_args()
    uvicorn.run(create_app(json.loads(Path(a.config).read_text())), host=a.host, port=a.port, uds=a.uds, access_log=False)

if __name__ == '__main__': main()
