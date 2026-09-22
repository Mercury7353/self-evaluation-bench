"""Grade durable terminal replies, independently of inference worker failures."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from codex_frozen_completion import dump, grade, parse_events

def replay(config):
    cfg=json.loads(Path(config).read_text());out=Path(cfg['output']);suite=Path(cfg['suite'])
    manifest=json.loads((suite/'evaluation.json').read_text());spec=json.loads((suite/'grader_spec.json').read_text())
    summaries={};pending=0
    for model in cfg['models']:
        rows=[]
        for item in manifest['items']:
            location=out/model['id']/item['id'];saved=location/'item.json'
            row=json.loads(saved.read_text()) if saved.exists() else None
            if not row or row.get('execution_status')!='completed':
                attempts=sorted(location.glob('attempt-*/events.jsonl'))
                for events in attempts:
                    parsed=parse_events(events.read_text())
                    if not parsed['terminal'] or parsed['tools']:continue
                    answer_file=events.parent/'answer.txt'
                    answer=answer_file.read_text() if answer_file.exists() else parsed['answer']
                    row={'id':item['id'],'execution_status':'completed','route':'codex-chatgpt-login',
                         'requested_model':model['model'],'effort':model['effort'],
                         'original_max_output_tokens':spec.get('tokens',{}).get(item['id'],1536),
                         'original_output_cap_enforced':False,'usage':parsed['usage'],
                         'evidence':str(events.parent),'answer_sha256':hashlib.sha256(answer.encode()).hexdigest(),
                         'offline_replay':True,**grade(cfg['rootfs'],suite,item['id'],answer)}
                    dump(saved,row);break
            if row:rows.append(row)
        complete={r['id']:r for r in rows if r.get('execution_status')=='completed'}
        domains={}
        for d,agg in manifest['domain_aggregations'].items():
            w=agg['weights'];observed={i:v for i,v in w.items() if i in complete};den=sum(observed.values())
            domains[d]={'completed':len(observed),'expected':len(w),'complete':len(observed)==len(w),
                        'weighted_coverage':den/sum(w.values()),
                        'observed_score':sum(complete[i]['score']*v for i,v in observed.items())/den if den else None}
        usage={k:sum((r.get('usage') or {}).get(k,0) for r in rows) for k in ('input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens')}
        price=model['price'];cost=((usage['input_tokens']-usage['cached_input_tokens'])*price['input']+usage['cached_input_tokens']*price['input']*price.get('cache_read_multiplier',1)+usage['output_tokens']*price['output'])/1e6
        result={'model':model,'completed':len(complete),'expected':len(manifest['items']),'items':rows,
                'domains':domains,'usage':usage,'api_equivalent_estimate_usd':cost,'estimate_is_invoice':False,
                'api_ledger_modified':False,'route':'codex-chatgpt-login','updated':time.time()}
        dump(out/model['id']/'replayed-result.json',result)
        summaries[model['id']]={k:result[k] for k in ('completed','expected','domains','usage','api_equivalent_estimate_usd')}
        pending+=len(manifest['items'])-len(complete)
    state={'phase':'stopped_by_user' if (out/'STOP').exists() else ('completed' if pending==0 else 'running'),'updated':time.time(),'models':summaries,
           'authorization':cfg['authorization'],'result_source':'replayed-result.json; responses preserved before grading',
           'original_controller_grading_error':'/usr/bin/python3 is Python 3.6: subprocess.run(text=...) rejected; inferencing continues, grading replay uses Python 3.12+.'}
    dump(out/'replay-state.json',state)
    return state

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--watch',action='store_true');a=p.parse_args()
    deadline=time.time()+21600
    while True:
        state=replay(a.config)
        print(state['phase'],{m:r['completed'] for m,r in state['models'].items()},flush=True)
        if not a.watch or state['phase'] in ('completed','stopped_by_user') or time.time()>=deadline:break
        time.sleep(15)
