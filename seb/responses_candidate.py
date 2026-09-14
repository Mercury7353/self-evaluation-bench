"""Strict Anthropic text/tool adapter for native Responses candidate models.

Wire translation only: retries, reservations and artifact ownership stay in the
candidate execution policy. Raw upstream usage is retained for billing.
"""
import hashlib
import json
import math
from pathlib import Path

def text_content(content):
    if isinstance(content, str):
        return content
    if content is None:
        return ''
    parts = []
    for block in content:
        if block.get('type') != 'text':
            raise ValueError('Only text tool results/system content are supported')
        parts.append(block.get('text', ''))
    return '\n'.join(parts)


def to_openai(body, model, effort, reasoning_by_call=None, messages_by_call=None):
    if body.get('model') != model:
        raise ValueError('Unexpected candidate model alias')
    inputs, seen_reasoning = [], set()
    reasoning_by_call, messages_by_call = reasoning_by_call or {}, messages_by_call or {}
    if body.get('system'):
        inputs.append({'role':'system','content':text_content(body['system'])})
    for message in body.get('messages', []):
        role, content = message['role'], message['content']
        if role not in ('user','assistant'):
            raise ValueError('Unsupported message role')
        if isinstance(content,str):
            inputs.append({'role':role,'content':content})
            continue
        calls=[b['id'] for b in content if b.get('type')=='tool_use'] if role=='assistant' else []
        prior=next((messages_by_call[c] for c in calls if c in messages_by_call),None)
        if prior:
            normalized=[{k:v for k,v in block.items() if k!='cache_control'} for block in content]
            if normalized!=prior['content']:
                raise ValueError('Provider assistant history was changed; cannot safely replay its state')
            inputs.extend(prior['output'])
            seen_reasoning.update(i['id'] for i in prior['output'] if i['type']=='reasoning')
            continue
        texts=[]
        def flush():
            if texts:
                inputs.append({'role':role,'content':'\n'.join(texts)})
                texts.clear()
        for block in content:
            kind=block.get('type')
            if kind=='text':
                texts.append(block.get('text',''))
            elif kind=='tool_use' and role=='assistant':
                flush()
                for item in reasoning_by_call.get(block['id'],[]):
                    if item['id'] not in seen_reasoning:
                        inputs.append(item);seen_reasoning.add(item['id'])
                inputs.append({'type':'function_call','call_id':block['id'],'name':block['name'],
                               'arguments':json.dumps(block['input'])})
            elif kind=='tool_result' and role=='user':
                flush();output=text_content(block.get('content'))
                if block.get('is_error'):output='[tool error]\n'+output
                inputs.append({'type':'function_call_output','call_id':block['tool_use_id'],'output':output})
            else:
                raise ValueError('Unsupported content block: '+str(kind))
        flush()
    request = {'model': model, 'input': inputs, 'reasoning': {'effort': effort},
               'max_output_tokens': body.get('max_tokens', 32000), 'stream': False, 'store': False,
               'include': ['reasoning.encrypted_content']}
    if body.get('tools'):
        tools = []
        for tool in body['tools']:
            if 'input_schema' not in tool:
                raise ValueError('Only schema-based function tools are supported')
            tools.append({'type': 'function', 'name': tool['name'],
                          'description': tool.get('description', ''), 'parameters': tool['input_schema'],
                          'strict': False})
        request['tools'] = tools
        choice = body.get('tool_choice', {'type': 'auto'})
        kind = choice.get('type', 'auto')
        if kind == 'tool':
            request['tool_choice'] = {'type': 'function', 'name': choice['name']}
        elif kind in ('auto', 'any', 'none'):
            request['tool_choice'] = {'any': 'required'}.get(kind, kind)
        else:
            raise ValueError('Unsupported tool choice')
        request['parallel_tool_calls'] = not choice.get('disable_parallel_tool_use', False)
    return request


class CandidateAdapter:
    def __init__(self, root, backend, alias, namespace):
        self.alias, self.backend = alias, backend
        scope=json.dumps({'session':namespace,'model':backend['model'],'effort':backend['effort']},sort_keys=True)
        self.root = Path(root) / 'responses-state' / hashlib.sha256(scope.encode()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, call_id):
        return self.root / (hashlib.sha256(call_id.encode()).hexdigest() + '.json')

    def prepare(self, body, price):
        from .gateway import reservation
        from .native_responses import usage_cost
        allowed = {'model','messages','system','max_tokens','tools','tool_choice','stream',
                   'output_config','thinking','metadata','service_tier','context_management'}
        if set(body) - allowed:
            raise ValueError('Unsupported Responses adapter request fields: ' + ', '.join(sorted(set(body)-allowed)))
        effort = self.backend['effort']
        output_config = body.get('output_config') or {}
        if set(output_config)-{'effort'} or output_config.get('effort', effort) != effort:
            raise ValueError('Candidate effort differs from the frozen Responses configuration')
        thinking = body.get('thinking')
        if thinking and thinking != {'type':'adaptive'}:
            raise ValueError('This candidate uses a frozen native effort, not an Anthropic thinking budget')
        context = body.get('context_management')
        # Claude Code asks Anthropic to keep all thinking. This is already our
        # replay policy; edits that would remove state have no native equivalent.
        if context not in (None, {}, {'edits':[]},
                {'edits':[{'type':'clear_thinking_20251015','keep':'all'}]}):
            raise ValueError('Responses candidate history cannot use Anthropic context edits')
        limits = self.backend['native_limits']
        if type(body.get('max_tokens')) is not int or not 0 < body['max_tokens'] <= limits['max_output_tokens']:
            raise ValueError('Output request exceeds the candidate model limit')
        by_call, by_message, opaque_bounds = {}, {}, {}
        for message in body.get('messages', []):
            content = message.get('content')
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get('type') != 'tool_use':
                    continue
                path = self.path(block['id'])
                if path.exists():
                    prior = json.loads(path.read_text())
                    if prior['tool'] != {'name': block['name'], 'input': block['input']}:
                        raise ValueError('A provider tool-call ID was reused with different content')
                    by_call[block['id']] = prior['reasoning']
                    by_message[block['id']] = prior
                    for item in prior['reasoning']:
                        opaque_bounds[item['id']] = prior['opaque_tokens_bound']
        wire = to_openai(body, self.alias, effort, by_call, by_message)
        wire['model'] = self.backend['model']
        amount = reservation({**wire, 'max_tokens': wire['max_output_tokens']}, price)
        if opaque_bounds:
            # The provider's preceding input+output usage bounds the state we
            # retained. Client-supplied encrypted state is never accepted here.
            tokens = len(json.dumps(wire, ensure_ascii=False).encode()) + 4096
            tokens += min(limits['max_context_tokens'], sum(opaque_bounds.values()))
            # Apply the long-context tariff to the entire request, including its
            # output. Summing two separately priced bounds misses the threshold.
            amount = max(amount, usage_cost({'input_tokens': tokens,
                'output_tokens': math.ceil(wire['max_output_tokens'] * price.get('reservation_output_headroom', 1)),
                'input_tokens_details': {'cache_write_tokens': tokens}}, price))
        return wire, amount

    def translate(self, response, streaming):
        message = to_anthropic(response, self.alias)
        by_call = {}
        remember_reasoning(response, by_call)
        usage = response['usage']
        for block in message['content']:
            if block['type'] != 'tool_use':
                continue
            record = {'reasoning': by_call[block['id']], 'opaque_tokens_bound': usage['input_tokens'] + usage['output_tokens'],
                      'tool': {'name': block['name'], 'input': block['input']},
                      'content':message['content'],'output':response['output']}
            path = self.path(block['id'])
            if path.exists() and json.loads(path.read_text()) != record:
                raise ValueError('Conflicting provider tool-call ID')
            temporary = path.with_suffix('.tmp'); temporary.write_text(json.dumps(record)); temporary.replace(path)
        if not streaming:
            return json.dumps(message, ensure_ascii=False).encode(), 'application/json'
        return ''.join('event: '+event['type']+'\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n'
                       for event in events(message)).encode(), 'text/event-stream'


def remember_reasoning(response, by_call):
    # Replay opaque reasoning across stateless tool turns. Never substitute a
    # summary for hidden state, and never inject another experiment's context.
    reasoning = [item for item in response.get('output', []) if item['type'] == 'reasoning']
    if any(item['type'] == 'function_call' for item in response.get('output', [])) and any(not item.get('encrypted_content') for item in reasoning):
        raise ValueError('Missing encrypted reasoning needed for stateless tool replay')
    for item in response.get('output', []):
        if item['type'] == 'function_call':
            by_call[item['call_id']] = reasoning


def to_anthropic(response, model):
    if response.get('status') not in ('completed', 'incomplete'):
        raise ValueError('Upstream did not complete a model response')
    content = []
    for item in response['output']:
        if item['type'] == 'message':
            for block in item['content']:
                if block['type'] == 'output_text':
                    content.append({'type': 'text', 'text': block['text']})
                elif block['type'] == 'refusal':
                    content.append({'type': 'text', 'text': block['refusal']})
                else:
                    raise ValueError('Unsupported output content type')
        elif item['type'] == 'function_call':
            content.append({'type': 'tool_use', 'id': item['call_id'], 'name': item['name'],
                            'input': json.loads(item['arguments'])})
        elif item['type'] != 'reasoning':
            raise ValueError('Unsupported output item type')
    usage = response.get('usage')
    if not usage or 'input_tokens' not in usage or 'output_tokens' not in usage:
        raise ValueError('Missing upstream token usage; do not report zero cost')
    details = usage.get('input_tokens_details') or {}
    cached, written = details.get('cached_tokens', 0), details.get('cache_write_tokens', 0)
    if cached + written > usage['input_tokens']:
        raise ValueError('Inconsistent upstream token accounting')
    return {'id': response['id'], 'type': 'message', 'role': 'assistant', 'model': model,
            'content': content, 'stop_reason': 'max_tokens' if response.get('status') == 'incomplete'
            else 'tool_use' if any(b['type'] == 'tool_use' for b in content) else 'end_turn', 'stop_sequence': None,
            'usage': {'input_tokens': usage['input_tokens'] - cached - written,
                      'cache_read_input_tokens': cached, 'cache_creation_input_tokens': written,
                      'output_tokens': usage['output_tokens']}}


def events(message):
    start = dict(message, content=[], stop_reason=None)
    yield {'type': 'message_start', 'message': start}
    for index, block in enumerate(message['content']):
        if block['type'] == 'tool_use':
            first = dict(block, input={})
            delta = {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}
        else:
            first = {'type': 'text', 'text': ''}
            delta = {'type': 'text_delta', 'text': block['text']}
        yield {'type': 'content_block_start', 'index': index, 'content_block': first}
        yield {'type': 'content_block_delta', 'index': index, 'delta': delta}
        yield {'type': 'content_block_stop', 'index': index}
    yield {'type': 'message_delta', 'delta': {'stop_reason': message['stop_reason'], 'stop_sequence': None},
           'usage': message['usage']}
    yield {'type': 'message_stop'}
