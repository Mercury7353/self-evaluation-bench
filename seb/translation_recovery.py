"""Offline repair of terminal text Chat responses rejected by cache metadata.

No provider calls, answer changes, token reconciliation or budget changes.
Original operation bytes and ledger row are archived before publication. Run
only against terminal operations; the gateway never rewrites a terminal result.
"""
import hashlib
import json
import tempfile
import time
from pathlib import Path

from .chat_candidate import CandidateAdapter
from .ledger import Ledger


def recover_empty_cache_response(root, call_id):
    """Compatibility entry point restricted to max-token empty answers."""
    return recover_terminal_cache_response(root,call_id,empty_only=True)


def recover_terminal_cache_response(root, call_id, *, empty_only=False):
    root=Path(root);ledger=Ledger(root/'ledger.sqlite')
    wire=root/'wire'/call_id
    meta=json.loads((wire/'meta.json').read_text())
    raw=(wire/'response.body').read_bytes()
    if meta.get('response_sha256')!=hashlib.sha256(raw).hexdigest():
        raise ValueError('Response checksum mismatch')
    if meta.get('http_status')!=200 or meta.get('state')!='response_translation_error':
        raise ValueError('Not a completed HTTP200 translation failure')
    value=json.loads(raw);choices=value.get('choices',[])
    if len(choices)!=1 or choices[0].get('finish_reason') not in (('length',) if empty_only else ('stop','length')):
        raise ValueError('Only verified terminal text answers are eligible')
    message=choices[0].get('message',{})
    if empty_only and message.get('content') not in ('',None):
        raise ValueError('Only empty answers are eligible')
    if (message.get('content') is not None and not isinstance(message['content'],str)) or message.get('tool_calls') or message.get('refusal'):
        raise ValueError('Only text answers without tool calls or refusal are eligible')
    usage=value.get('usage',{});cached=(usage.get('prompt_tokens_details') or {}).get('cached_tokens')
    if type(cached) is not int or type(usage.get('prompt_tokens')) is not int or cached<=usage['prompt_tokens']:
        raise ValueError('Not the verified inconsistent cache-count case')
    body=json.loads((wire/'request.body').read_text())
    if body.get('stream'):raise ValueError('Streaming recovery is unsupported')
    operation=root/'operations'/hashlib.sha256((meta['wallet']+'\0'+meta['operation_id']).encode()).hexdigest()
    previous=json.loads((operation/'result.json').read_text())
    archive=root/'translation-recovery'/call_id
    if previous.get('offline_translation_recovery')==call_id:
        return {'call_id':call_id,'status':'already_recovered'}
    if previous.get('http_status')!=422 or previous.get('headers',{}).get('x-seb-request-id')!=call_id:
        raise ValueError('Operation is not the matching terminal failure')
    if previous.get('attempts',[])[-1:]!=[{'id':call_id,'state':'response_translation_error','retryable':False}]:
        raise ValueError('Unexpected terminal attempt')
    with tempfile.TemporaryDirectory() as temp:
        adapter=CandidateAdapter(temp,{'model':value.get('model'),'effort':'max','allow_unreconciled_usage':True},meta['model'],'offline')
        translated,media=adapter.translate(value,False)
    with ledger.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM calls WHERE id=?',(call_id,)).fetchone()
        if row is None or row['state']!='response_translation_error' or row['charged'] is not None:
            raise ValueError('Ledger is not the expected unsettled translation failure')
        if row['wallet']!=meta['wallet'] or row['model']!=meta['model'] or json.loads(row['usage'])!=usage:
            raise ValueError('Ledger evidence mismatch')
        archive.mkdir(parents=True,exist_ok=False)
        for name in ('result.json','response.body'):
            (archive/name).write_bytes((operation/name).read_bytes())
        audit={'call_id':call_id,'recovered_at':time.time(),'original_ledger_row':dict(row),
            'raw_response_sha256':meta['response_sha256'],'translated_sha256':hashlib.sha256(translated).hexdigest(),
            'answer_kind':'empty' if not message.get('content') else 'text',
            'reason':'Restore original terminal answer; preserve all metering fields and raw wire evidence'}
        (archive/'audit.json').write_text(json.dumps(audit,indent=2))
        updated=previous|{'http_status':200,'media_type':media,'offline_translation_recovery':call_id}
        updated['attempts']=[a|{'state':'completed'} if a['id']==call_id else a for a in previous['attempts']]
        # The old failed response is already delivered. Publish the recovered
        # envelope before allowing the collector to discover completed evidence.
        temporary=operation/'recovered-response.tmp';temporary.write_bytes(translated);temporary.replace(operation/'response.body')
        temporary=operation/'recovered-result.tmp';temporary.write_text(json.dumps(updated,indent=2));temporary.replace(operation/'result.json')
        db.execute("UPDATE calls SET state='completed' WHERE id=?",(call_id,))
    return {'call_id':call_id,'status':'recovered','archive':str(archive)}
