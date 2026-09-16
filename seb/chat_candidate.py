"""Translate the common candidate text/tool interface to Chat Completions.

Native assistant tool state is retained verbatim, scoped to the caller. Only
wire conversion lives here; the existing gateway owns metering and retries.
"""
import copy
import hashlib
import json
import math
from pathlib import Path

from .responses_candidate import events, text_content
from .usage import chat_output_tokens


class CandidateAdapter:
    def __init__(self, root, backend, alias, namespace):
        self.alias, self.backend = alias, backend
        scope = json.dumps({'session':namespace,'model':backend['model'],
                            'effort':backend['effort']},sort_keys=True)
        self.root = Path(root)/'chat-state'/hashlib.sha256(scope.encode()).hexdigest()
        self.root.mkdir(parents=True,exist_ok=True)

    def path(self, call_id):
        return self.root/(hashlib.sha256(call_id.encode()).hexdigest()+'.json')

    def prepare(self, body, price):
        from .gateway import reservation
        allowed = {'model','messages','system','max_tokens','tools','tool_choice','stream',
                   'output_config','thinking','metadata','service_tier','context_management',
                   'temperature','top_p','stop_sequences'}
        if set(body)-allowed:
            raise ValueError('Unsupported Chat adapter fields: '+', '.join(sorted(set(body)-allowed)))
        if body['model'] != self.alias:
            raise ValueError('Unexpected candidate alias')
        effort = self.backend['effort']
        output = body.get('output_config') or {}
        if set(output)-{'effort'} or output.get('effort',effort)!=effort:
            raise ValueError('Candidate effort differs from frozen Chat configuration')
        if body.get('thinking') not in (None,{}, {'type':'adaptive'}):
            raise ValueError('Chat candidate uses frozen effort, not an Anthropic thinking budget')
        if body.get('context_management') not in (None,{}, {'edits':[]},
                {'edits':[{'type':'clear_thinking_20251015','keep':'all'}]}):
            raise ValueError('Chat candidate history cannot use Anthropic context edits')
        if type(body.get('max_tokens')) is not int or not 0<body['max_tokens']<=self.backend['native_limits']['max_output_tokens']:
            raise ValueError('Output request exceeds candidate model limit')
        messages=[];opaque_bounds={}
        if body.get('system'):
            messages.append({'role':'system','content':text_content(body['system'])})
        for message in body.get('messages',[]):
            role,content=message['role'],message['content']
            if role not in ('user','assistant'):
                raise ValueError('Unsupported message role')
            if isinstance(content,str):
                messages.append({'role':role,'content':content});continue
            normalized=[{k:v for k,v in b.items() if k!='cache_control'} for b in content]
            calls=[b for b in normalized if b.get('type')=='tool_use']
            if calls:
                if role!='assistant':raise ValueError('Tool calls must be assistant content')
                paths=[self.path(b['id']) for b in calls]
                if not all(p.is_file() for p in paths):
                    raise ValueError('Missing native assistant tool state in this candidate session')
                records=[json.loads(p.read_text()) for p in paths]
                if any(r['content']!=normalized or r!=records[0] for r in records):
                    raise ValueError('Provider assistant history was changed')
                native=records[0]['native_message']
                encoded=json.dumps(native,sort_keys=True)
                if 'signature' in encoded:
                    opaque_bounds[hashlib.sha256(encoded.encode()).hexdigest()]=records[0]['opaque_tokens_bound']
                messages.append(native);continue
            texts=[]
            def flush():
                if texts:
                    messages.append({'role':role,'content':'\n'.join(texts)});texts.clear()
            for block in normalized:
                kind=block.get('type')
                if kind=='text':texts.append(block.get('text',''))
                elif kind=='tool_result' and role=='user':
                    flush();value=text_content(block.get('content'))
                    if block.get('is_error'):value='[tool error]\n'+value
                    messages.append({'role':'tool','tool_call_id':block['tool_use_id'],'content':value})
                else:raise ValueError('Unsupported content block: '+str(kind))
            flush()
        wire={'model':self.backend['model'],'messages':messages,'max_tokens':body['max_tokens'],
              'reasoning_effort':effort,'stream':False}
        for name in ('temperature','top_p'):
            if name in body:wire[name]=body[name]
        if 'stop_sequences' in body:wire['stop']=body['stop_sequences']
        if body.get('tools'):
            tools=[]
            for tool in body['tools']:
                if 'input_schema' not in tool or tool.get('type') not in (None,'custom'):
                    raise ValueError('Only schema-based function tools are supported')
                tools.append({'type':'function','function':{'name':tool['name'],
                    'description':tool.get('description',''),'parameters':tool['input_schema']}})
            wire['tools']=tools
            choice=body.get('tool_choice') or {'type':'auto'}
            if set(choice)-{'type','name'}:
                raise ValueError('Unsupported Chat tool-choice option')
            kind=choice.get('type','auto')
            if kind=='tool':wire['tool_choice']={'type':'function','function':{'name':choice['name']}}
            elif kind in ('auto','any','none'):wire['tool_choice']={'any':'required'}.get(kind,kind)
            else:raise ValueError('Unsupported tool choice')
        amount=reservation(wire,price)
        if opaque_bounds:
            from .native_responses import usage_cost
            tokens=len(json.dumps(wire,ensure_ascii=False).encode())+4096
            tokens+=min(self.backend['native_limits']['max_context_tokens'],sum(opaque_bounds.values()))
            amount=max(amount,usage_cost({'input_tokens':tokens,
                'input_tokens_details':{'cache_write_tokens':tokens},
                'output_tokens':math.ceil(wire['max_tokens']*price.get('reservation_output_headroom',1))},price))
        return wire,amount

    def translate(self, response, streaming):
        choices=response.get('choices')
        if not isinstance(choices,list) or len(choices)!=1:
            raise ValueError('Exactly one Chat choice required')
        choice=choices[0];native=copy.deepcopy(choice['message'])
        if native.get('role')!='assistant':raise ValueError('Expected assistant response')
        content=[]
        text=native.get('content')
        if text is not None and not isinstance(text,str):
            raise ValueError('Unsupported non-text Chat response')
        if text:content.append({'type':'text','text':text})
        if native.get('refusal'):
            content.append({'type':'text','text':native['refusal']})
        for call in native.get('tool_calls') or []:
            if call.get('type')!='function':raise ValueError('Unsupported Chat tool call')
            args=json.loads(call['function']['arguments'])
            if not isinstance(args,dict):raise ValueError('Tool arguments must be an object')
            content.append({'type':'tool_use','id':call['id'],'name':call['function']['name'],'input':args})
        stop=choice.get('finish_reason')
        if stop not in ('stop','length','tool_calls','content_filter'):
            raise ValueError('Chat response lacks supported terminal finish_reason')
        usage=response.get('usage') or {};output=chat_output_tokens(usage)
        if output is None and self.backend.get('allow_unreconciled_usage'):
            # Raw provider usage is preserved and gateway charge stays unknown.
            # Translation only needs explicit nonnegative input/output counts.
            if all(type(usage.get(k)) is int and usage[k]>=0 for k in ('prompt_tokens','completion_tokens')):
                output=usage['completion_tokens']
        if output is None:raise ValueError('Missing or ambiguous native token usage')
        details=usage.get('prompt_tokens_details') or {}
        cached=details.get('cached_tokens') or 0;written=details.get('cache_write_tokens') or 0
        invalid_cache=any(type(n) is not int or n<0 for n in (cached,written)) or cached+written>usage['prompt_tokens']
        if invalid_cache and not self.backend.get('allow_unreconciled_usage'):
            raise ValueError('Inconsistent native input usage')
        message={'id':response['id'],'type':'message','role':'assistant','model':self.alias,
                 'content':content,'stop_reason':'max_tokens' if stop=='length' else
                    'tool_use' if any(b['type']=='tool_use' for b in content) else 'end_turn',
                 'stop_sequence':None,'usage':{'input_tokens':usage['prompt_tokens']-cached-written,
                    'cache_read_input_tokens':cached,'cache_creation_input_tokens':written,'output_tokens':output}}
        if invalid_cache:
            # Answer delivery does not require inventing a valid cache breakdown.
            # Metering uses the untouched upstream usage, not this envelope.
            message['usage']={'input_tokens':usage['prompt_tokens'],'output_tokens':output}
            message['usage_status']='unreconciled'
            message['native_usage']=copy.deepcopy(usage)
        record={'content':content,'native_message':native,'opaque_tokens_bound':usage['prompt_tokens']+output}
        paths=[self.path(b['id']) for b in content if b['type']=='tool_use']
        for path in paths:
            if path.exists() and json.loads(path.read_text())!=record:
                raise ValueError('Conflicting provider tool-call ID')
        for path in paths:
            temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(record));temporary.replace(path)
        if not streaming:return json.dumps(message,ensure_ascii=False).encode(),'application/json'
        return ''.join('event: '+e['type']+'\ndata: '+json.dumps(e,ensure_ascii=False)+'\n\n'
                       for e in events(message)).encode(),'text/event-stream'
