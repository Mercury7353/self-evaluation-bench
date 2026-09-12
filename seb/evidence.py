"""Validate that each scored item uses its own budget scope."""
import hashlib
import json
from pathlib import Path

def check_budget_evidence(result, manifest, ledger, scope, jobs):
    declared={i['id']:i.get('budget_group',i['id']) for i in manifest['items']}
    with ledger.connect() as db:
        for item in result['items']:
            expected='item:'+scope+':'+hashlib.sha256(declared[item['id']].encode()).hexdigest()
            for ev in item.get('evidence',[]):
                if ev['kind']=='llm':
                    found=db.execute('SELECT 1 FROM call_budget_scopes WHERE call_id=? AND scope=?',
                                     (ev['id'],expected)).fetchone()
                else:
                    request=json.loads((Path(jobs)/ev['id']/'request.json').read_text())
                    found=expected in request.get('budget_scopes',{})
                if not found:raise ValueError('Evidence belongs to another item budget or execution')
