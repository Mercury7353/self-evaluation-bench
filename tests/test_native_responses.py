import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from seb.billing import cache_adjusted_cost
from seb.gateway import create_app, cost
from seb.native_responses import request_bound, usage_cost


PRICE = {'input': 4, 'output': 20, 'cache_read_multiplier': .1, 'cache_write_multiplier': 1.25,
         'long_context': {'threshold_tokens': 272000, 'input_multiplier': 2, 'output_multiplier': 1.5}}


@pytest.fixture
def upstream():
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            received.append((self.path, body, dict(self.headers)))
            status, payload, mime = self.server.respond(self.path, json.loads(body))
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers(); self.wfile.write(payload)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.respond = lambda path, body: (200, json.dumps({'status': 'completed',
        'usage': {'input_tokens': 100, 'output_tokens': 20, 'input_tokens_details': {'cached_tokens': 80}},
        'output': []}).encode(), 'application/json')
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield server, received
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def config(tmp_path, upstream, *, cap=10):
    key = tmp_path / 'provider.secret'; key.write_text('PROVIDER_KEY_NOT_FOR_TRACES')
    return {'artifacts': str(tmp_path / 'gateway'), 'key_file': str(key),
            'model_backends': {'researcher': {'model': 'gpt-5.6-sol', 'key_file': str(key),
                'upstream': f'http://127.0.0.1:{upstream.server_port}/v1'}},
            'prices': {'researcher': PRICE},
            'native_researcher': {'id': 'researcher', 'model': 'gpt-5.6-sol', 'effort': 'max',
                'max_output_tokens': 128000, 'max_context_tokens': 1050000},
            'tokens': {'native-test-key': {'wallet': 'designer', 'cap': cap, 'models': ['researcher'],
                'native_responses': True, 'deadline_epoch': time.time() + 120},
                'candidate-key': {'wallet': 'development', 'cap': 10, 'models': ['researcher']}}}


def payload(**extra):
    return {'model': 'gpt-5.6-sol', 'reasoning': {'effort': 'max'}, 'input': 'Hello',
            'max_output_tokens': 32768, **extra}


def test_native_cache_costs_are_model_specific_and_not_double_counted():
    usage = {'input_tokens': 1000, 'output_tokens': 20,
             'input_tokens_details': {'cached_tokens': 800, 'cache_write_tokens': 100}}
    for price in (PRICE, PRICE | {'input': 10, 'output': 50}, PRICE | {'input': 5, 'output': 30}):
        assert usage_cost(usage, price) == pytest.approx((1025 * price['input'] + 20 * price['output']) / 1e6)
        discounted = (305 * price['input'] + 20 * price['output']) / 1e6
        assert cache_adjusted_cost(usage, price) == pytest.approx(discounted)
        assert cost(usage, price) == usage_cost(usage, price)
    usage['input_tokens'] = 300000
    assert usage_cost(usage, PRICE) == pytest.approx((300025 * 4 * 2 + 20 * 20 * 1.5) / 1e6)
    usage['input_tokens'] = 10
    with pytest.raises(ValueError): usage_cost(usage, PRICE)


@pytest.mark.parametrize('change', [
    {'previous_response_id': 'remote'}, {'conversation': 'remote'}, {'background': True},
    {'tools': [{'type': 'web_search'}]}, {'input': [{'type': 'input_image', 'image_url': 'https://example.com'}]},
    {'reasoning': {'effort': 'low'}}, {'model': 'other'}, {'max_output_tokens': 131072},
    {'service_tier': 'priority'},
])
def test_unbounded_or_unfrozen_native_requests_never_call_provider(tmp_path, upstream, change):
    server, received = upstream
    app = create_app(config(tmp_path, server))
    with TestClient(app) as client:
        response = client.post('/v1/responses', json=payload(**change), headers={'x-api-key': 'native-test-key'})
    assert response.status_code == 400
    assert not received and app.state.ledger.status('designer')[0]['calls'] == 0


def test_native_token_cannot_bypass_protocol_or_model_scope(tmp_path, upstream):
    server, received = upstream
    app = create_app(config(tmp_path, server))
    with TestClient(app) as client:
        assert client.post('/v1/responses', json=payload(), headers={'x-api-key': 'candidate-key'}).status_code == 403
        assert client.post('/anthropic/v1/messages', json={'model': 'researcher', 'max_tokens': 100},
                           headers={'x-api-key': 'native-test-key'}).status_code == 403
    assert not received


def test_native_preserves_exact_wire_and_idempotent_operation(tmp_path, upstream):
    server, received = upstream
    app = create_app(config(tmp_path, server))
    raw = b'{"model":"gpt-5.6-sol", "reasoning":{"effort":"max"}, "input":"Hello", "max_output_tokens":32768}'
    headers = {'Authorization': 'Bearer native-test-key', 'x-seb-operation-id': 'original-operation'}
    with TestClient(app) as client:
        response = client.post('/v1/responses', content=raw, headers=headers)
        replay = client.post('/v1/responses', content=raw, headers=headers)
    assert response.status_code == 200 and replay.content == response.content
    assert replay.headers['x-seb-replayed'] == 'true'
    assert len(received) == 1 and received[0][0] == '/v1/responses' and received[0][1] == raw
    folder = Path(app.state.ledger.path).parent / 'native-wire' / response.headers['x-seb-request-id']
    assert (folder / 'request.body').read_bytes() == raw
    assert (folder / 'response.body').read_bytes() == response.content
    for path in folder.iterdir():
        assert b'PROVIDER_KEY_NOT_FOR_TRACES' not in path.read_bytes()
        assert b'native-test-key' not in path.read_bytes()
    wallet = app.state.ledger.status('designer')[0]
    assert wallet['calls'] == 1 and wallet['outstanding'] == 0
    assert wallet['charged'] == pytest.approx(.0008)


def test_native_incomplete_stream_keeps_reservation_and_is_not_retried(tmp_path, upstream):
    server, received = upstream
    server.respond = lambda *_: (200, b'data: {"type":"response.created","response":{}}\n\n', 'text/event-stream')
    app = create_app(config(tmp_path, server))
    headers = {'x-api-key': 'native-test-key', 'x-seb-operation-id': 'interrupted'}
    with TestClient(app) as client:
        response = client.post('/v1/responses', json=payload(stream=True), headers=headers)
        replay = client.post('/v1/responses', json=payload(stream=True), headers=headers)
    assert response.status_code == 200 and replay.content == response.content
    assert len(received) == 1
    assert app.state.ledger.status('designer')[0]['outstanding'] > 0


def test_native_compaction_and_opaque_history_are_bounded(tmp_path, upstream):
    server, received = upstream
    cfg = config(tmp_path, server)
    server.respond = lambda *_: (200, json.dumps({'object': 'response.compaction', 'output': [],
        'usage': {'input_tokens': 100, 'output_tokens': 20}}).encode(), 'application/json')
    body = payload(input=[{'type': 'compaction', 'encrypted_content': 'opaque'}])
    amount = request_bound(body, cfg['native_researcher'], PRICE, compact=True)
    assert amount >= usage_cost({'input_tokens': 1050000, 'output_tokens': 32768,
                                'input_tokens_details': {'cache_write_tokens': 1050000}}, PRICE)
    cfg['tokens']['native-test-key']['cap'] = 100
    app = create_app(cfg)
    with TestClient(app) as client:
        response = client.post('/v1/responses/compact', json=body, headers={'x-api-key': 'native-test-key'})
    assert response.status_code == 200 and received[0][0] == '/v1/responses/compact'
    assert app.state.ledger.status('designer')[0]['outstanding'] == 0


def test_native_wallet_and_deadline_reject_before_provider(tmp_path, upstream):
    server, received = upstream
    cfg = config(tmp_path, server, cap=.001)
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.post('/v1/responses', json=payload(), headers={'x-api-key': 'native-test-key'}).status_code == 402
        cfg['tokens']['native-test-key']['deadline_epoch'] = 1
        assert client.post('/v1/responses', json=payload(), headers={'x-api-key': 'native-test-key'}).status_code == 409
    assert not received


def test_native_quota_is_persistent_and_unknown_cost_is_retained(tmp_path, upstream):
    server, received = upstream
    server.respond = lambda *_: (429, b'{"error":{"code":"insufficient_quota"}}', 'application/json')
    cfg = config(tmp_path, server)
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.post('/v1/responses', json=payload(), headers={'x-api-key': 'native-test-key'}).status_code == 429
    with TestClient(create_app(cfg)) as client:
        assert client.post('/v1/responses', json=payload(), headers={'x-api-key': 'native-test-key'}).status_code == 403
    assert len(received) == 1 and app.state.ledger.status('designer')[0]['outstanding'] > 0


def test_native_actual_usage_overshoot_closes_wallet(tmp_path, upstream):
    server, received = upstream
    server.respond = lambda *_: (200, json.dumps({'status': 'completed', 'output': [],
        'usage': {'input_tokens': 999999, 'output_tokens': 999999}}).encode(), 'application/json')
    app = create_app(config(tmp_path, server))
    with TestClient(app) as client:
        client.post('/v1/responses', json=payload(), headers={'x-api-key': 'native-test-key'})
        response = client.post('/v1/responses', json=payload(), headers={'x-api-key': 'native-test-key'})
    assert response.status_code == 402 and len(received) == 1
    assert app.state.ledger.status('designer')[0]['cap'] == 0


@pytest.mark.parametrize('research_network', [False, True])
@pytest.mark.parametrize('model,effort', [('gpt-5.5-2026-04-23','xhigh'), ('gpt-5.6-sol','max'), ('gpt-6-astra','xhigh')])
def test_actual_codex_sdk_tool_roundtrip_inside_isolated_namespace(tmp_path, upstream, model, effort, research_network):
    root = os.environ.get('SEB_TEST_ROOT')
    if not root:
        pytest.skip('Set SEB_TEST_ROOT for real Codex SDK integration')
    import uvicorn
    from seb.codex_harness import launch_codex, runtime_files
    from seb.supervisor import check_designer_exit
    runtime_files()
    server, received = upstream
    canary = tmp_path / 'host-only'; canary.write_text('host file must stay outside')
    work = tmp_path / 'work'; work.mkdir()
    image = tmp_path / 'image'; shutil.copytree(root, image, symlinks=True)
    def respond(path, body):
        if len(received) == 1:
            import shlex
            script = ('import json,os,socket,urllib.request; '
                      "assert os.environ['TOKIO_WORKER_THREADS']=='4'; "
                      "assert json.load(urllib.request.urlopen(os.environ['SEB_GATEWAY_URL']+'/health'))['status']=='ok'; "
                      f'assert not os.path.exists({str(canary)!r}); '
                      f's=socket.socket();s.settimeout(1);assert (s.connect_ex(("127.0.0.1",{server.server_port}))==0)=={research_network}; '
                      'open("marker.txt","w").write("SDK_TOOL_OK"); print("SDK_TOOL_OK")')
            command = {'cmd': 'python -c ' + shlex.quote(script)}
            if any(t.get('name')=='exec_command' for t in body.get('tools',[])):
                item = {'type':'function_call','id':'fc_test','call_id':'call_test','name':'exec_command',
                        'arguments':json.dumps(command)}
            else:
                item = {'type': 'custom_tool_call', 'id': 'ctc_test', 'call_id': 'call_test',
                        'name': 'exec', 'namespace': 'functions',
                        'input': 'text(await tools.exec_command(' + json.dumps(command) + '));'}
        else:
            item = {'type': 'message', 'id': 'msg_test', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': 'SDK_PREFLIGHT_OK', 'annotations': []}]}
        response = {'id': 'resp_' + str(len(received)), 'object': 'response', 'created_at': 1,
                    'status': 'completed', 'model': body['model'], 'output': [item],
                    'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens':120,
                              'input_tokens_details': {'cached_tokens': 80}}}
        events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                  {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
                  {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                  {'type': 'response.completed', 'response': response}]
        return 200, ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events).encode(), 'text/event-stream'
    server.respond = respond
    cfg = config(tmp_path, server)
    cfg['native_researcher'].update(model=model, effort=effort)
    cfg['model_backends']['researcher']['model'] = cfg['native_researcher']['model']
    app = create_app(cfg)
    with tempfile.TemporaryDirectory(prefix='seb-codex-test-') as sockets:
        listener = socket.socket(socket.AF_UNIX)
        address = Path(sockets) / 'gateway.sock'; listener.bind(str(address))
        gateway = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = threading.Thread(target=gateway.run, kwargs={'sockets': [listener]}, daemon=True); thread.start()
        try:
            deadline = time.monotonic() + 10
            while not gateway.started and time.monotonic() < deadline: time.sleep(.02)
            assert gateway.started
            rc = launch_codex(image, work, tmp_path / 'trace', address, 'native-test-key',
                model, 'Use a shell to write marker.txt, then finish.', timeout=60, effort=effort, research_network=research_network)
            check_designer_exit(tmp_path / 'trace', rc, harness='codex')
            assert rc == 0, (tmp_path / 'trace/codex.stderr').read_text()
            assert (work / 'marker.txt').read_text() == 'SDK_TOOL_OK'
            assert len(received) == 2
            first, second = [json.loads(row[1]) for row in received]
            assert 'SDK_TOOL_OK' in json.dumps(second['input'])
            assert first['prompt_cache_key'] == second['prompt_cache_key']
            assert first['reasoning']['effort'] == second['reasoning']['effort'] == effort
            runtime=json.loads((tmp_path/'trace/runtime.json').read_text())
            assert runtime['model']==model and runtime['effort']==effort and runtime['sdk_version']=='0.153.4'
            assert runtime['tokio_worker_threads']==4
            assert runtime['isolated_network'] is (not research_network)
            assert len(runtime['files_sha256'])==5 and all(len(v)==64 for v in runtime['files_sha256'].values())
            wallet = app.state.ledger.status('designer')[0]
            assert wallet['calls'] == 2 and wallet['outstanding'] == 0
            assert not (tmp_path / 'trace/sdk.private.json').exists()
            assert not (tmp_path / 'trace/launch.private.json').exists()
            if model == 'gpt-6-astra':
                # Same actual SDK thread after a new process and rotated gateway
                # credential. Prior conversation and charges must survive.
                thread_id = json.loads((tmp_path/'trace/thread.json').read_text())['thread_id']
                cfg['tokens']['continued-native-key'] = cfg['tokens'].pop('native-test-key')
                rc = launch_codex(image, work, tmp_path/'continued-trace', address, 'continued-native-key',
                    model, 'Continue the same task after an infrastructure interruption.',
                    timeout=60, effort=effort, resume_thread=thread_id, research_network=research_network)
                check_designer_exit(tmp_path/'continued-trace', rc, harness='codex')
                assert json.loads((tmp_path/'continued-trace/thread.json').read_text())['thread_id'] == thread_id
                assert len(received) == 3 and 'SDK_PREFLIGHT_OK' in json.dumps(json.loads(received[-1][1])['input'])
                assert app.state.ledger.status('designer')[0]['calls'] == 3
                assert app.state.ledger.status('designer')[0]['outstanding'] == 0
        finally:
            gateway.should_exit = True; thread.join(timeout=5)
