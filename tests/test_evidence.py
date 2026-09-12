import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

from seb.ledger import Ledger,BudgetExceeded
from seb.execution_policy import DEFAULT_POLICY,reserve_with_backpressure
from seb.research import research_app
from seb.evidence import check_budget_evidence


def test_item_and_suite_caps_are_atomic_and_unknowns_survive_restart(tmp_path):
    ledger=Ledger(tmp_path/'ledger');ledger.wallet('dev',100)
    scopes={'suite':1,'item':.6}
    def reserve(i):
        try:ledger.reserve(str(i),'dev','m',.2,scopes=scopes);return True
        except BudgetExceeded:return False
    with ThreadPoolExecutor(max_workers=8) as pool:assert sum(pool.map(reserve,range(20)))==3
    ledger=Ledger(tmp_path/'ledger')
    with ledger.connect() as db:ids=[r[0] for r in db.execute('SELECT id FROM calls')]
    for ident in ids:ledger.finish(ident,None,{},'transport_unknown')
    with pytest.raises(BudgetExceeded):ledger.reserve('unknown','dev','m',.01,scopes=scopes)
    ledger.reserve('other','dev','m',.4,scopes={'suite':1,'other-item':.6})
    with pytest.raises(BudgetExceeded):ledger.reserve('overflow','dev','m',.01,scopes={'suite':1,'third':.6})
    assert ledger.status()[0]['outstanding']==pytest.approx(1)
    with pytest.raises(ValueError):ledger.reserve('reset','dev','m',.1,scopes={'item':10})


def test_scope_backpressure_waits_for_known_settlement_only(tmp_path):
    ledger=Ledger(tmp_path/'ledger');ledger.wallet('dev',100)
    ledger.reserve('a','dev','m',.9,scopes={'suite':1})
    async def go():
        async def settle():
            await asyncio.sleep(.03);ledger.finish('a',.01,{},'completed')
        t=asyncio.create_task(settle())
        await reserve_with_backpressure(ledger,'b','dev','m',.9,1,scopes={'suite':1});await t
    asyncio.run(go());ledger.finish('b',None,{},'transport_unknown')
    with pytest.raises(BudgetExceeded):asyncio.run(reserve_with_backpressure(ledger,'c','dev','m',.2,1,scopes={'suite':1}))


def test_shared_batch_evidence_cannot_be_reassigned_to_another_budget(tmp_path):
    ledger=Ledger(tmp_path/'ledger');ledger.wallet('w',10)
    scope='abc';budget='item:'+scope+':'+hashlib.sha256(b'batch').hexdigest()
    ledger.reserve('a'*32,'w','m',.5,scopes={budget:2});ledger.finish('a'*32,.1,{},'completed')
    manifest={'items':[{'id':'one','budget_group':'batch'},{'id':'two','budget_group':'batch'}]}
    result={'items':[{'id':i,'evidence':[{'kind':'llm','id':'a'*32}]} for i in ['one','two']]}
    check_budget_evidence(result,manifest,ledger,scope,tmp_path)
    manifest['items'][1]['budget_group']='different'
    with pytest.raises(ValueError,match='another item'):check_budget_evidence(result,manifest,ledger,scope,tmp_path)


def test_same_wallet_preflight_does_not_expose_heldout_models(tmp_path):
    key=tmp_path/'key';key.write_text('fixture')
    config={'key_file':str(key),'artifacts':str(tmp_path/'gateway'),'prices':{'dev':{'input':1,'output':1}},
            'tokens':{'token':{'wallet':'shared','cap':180,'models':['dev'],'research':True}},
            'evaluation_policy':DEFAULT_POLICY}
    folder=tmp_path/'research-jobs'/('a'*32);folder.mkdir(parents=True)
    (folder/'request.json').write_text(json.dumps({'wallet':'shared','model':'HIDDEN-MODEL'}))
    (folder/'result.json').write_text(json.dumps({'status':'ok','hidden_details':'private'}))
    with TestClient(research_app(config)) as client:
        headers={'x-api-key':'token'}
        assert client.get('/research/jobs',headers=headers).json()['jobs']==[]
        for path in ['', '/artifacts']:
            assert client.get('/research/jobs/'+('a'*32)+path,headers=headers).status_code==403
        assert client.post('/research/jobs/'+('a'*32)+'/cancel',headers=headers).status_code==403
