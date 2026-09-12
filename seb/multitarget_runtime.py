"""Research-facing multi-output contract and family-CV evaluator; Python standard library only."""
import argparse
import importlib.util
import json
import math
from numbers import Real
from pathlib import Path
import statistics as st


def load_predictor(path):
    spec=importlib.util.spec_from_file_location('submitted_predictor',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    if not callable(getattr(module,'fit',None)) or not callable(getattr(module,'predict',None)):
        raise ValueError('predictor.py requires fit(training_rows, target_metadata) and predict(fitted, observations)')
    return module


def selection(panel, value):
    if isinstance(value,dict):value=value.get('tasks')
    if not isinstance(value,list) or not value or any(not isinstance(x,str) for x in value):raise ValueError('Nonempty task list required')
    if len(set(value))!=len(value) or set(value)-set(panel['task_ids']):raise ValueError('Invalid/duplicate task IDs')
    cost=sum(panel['cost_weights'][x] for x in value)
    if cost>panel['max_cost_fraction']+1e-12:raise ValueError('Development source-cost fraction exceeds cap')
    return value,cost


def measurement(row,tasks):
    # No model name, family, target label, raw answer, or runtime cost fingerprint.
    return {t:row['scores'].get(t) for t in tasks}


def training(rows,tasks):
    return [{'observations':measurement(r,tasks),'targets':r['targets']} for r in rows]


def predict(module, train, observations, targets):
    fitted=module.fit(train,targets)
    output=[]
    for obs in observations:
        pred=module.predict(fitted,obs)
        if not isinstance(pred,dict) or set(pred)-set(targets):raise ValueError('Return a mapping keyed by known target IDs')
        clean={}
        for t,meta in targets.items():
            v=pred.get(t)
            if isinstance(v,Real) and not isinstance(v,bool):v={'score':v}
            if v is None or (isinstance(v,dict) and v.get('status')=='unsupported'):
                clean[t]={'status':'unsupported'};continue
            if not isinstance(v,dict):raise ValueError('Invalid prediction for '+t)
            score=v.get('score')
            if isinstance(score,bool) or not isinstance(score,Real) or not math.isfinite(score):raise ValueError('Finite predicted score required')
            score=float(score)
            if meta.get('scale',1)==1 and not 0<=score<=1:raise ValueError('Probability score outside [0,1]')
            item={'score':score}
            if 'lower' in v or 'upper' in v:
                lo,hi=v.get('lower'),v.get('upper')
                if any(isinstance(x,bool) or not isinstance(x,Real) or not math.isfinite(x) for x in [lo,hi]) or not lo<=score<=hi:
                    raise ValueError('Invalid interval')
                item.update(lower=float(lo),upper=float(hi))
            clean[t]=item
        output.append(clean)
    return output


def ranks(a):
    return [1+sum(x<v for x in a)+(sum(x==v for x in a)-1)/2 for v in a]


def corr(x,y):
    if len(x)<3:return None
    xm,ym=st.mean(x),st.mean(y)
    den=math.sqrt(sum((v-xm)**2 for v in x)*sum((v-ym)**2 for v in y))
    return sum((a-xm)*(b-ym) for a,b in zip(x,y))/den if den>1e-15 else None


def summarize(rows,targets):
    result={};objective=[]
    for t,meta in targets.items():
        relevant=[r for r in rows if t in r['targets']]
        valid=[r for r in relevant if 'score' in r['predictions'].get(t,{})]
        x=[r['predictions'][t]['score'] for r in valid];y=[r['targets'][t] for r in valid]
        out={'n_reference':len(relevant),'n_predicted':len(valid),'status':meta['status']}
        if x:
            pairs=[(i,j) for i in range(len(y)) for j in range(i) if y[i]!=y[j]]
            loss=st.mean(0 if (x[i]-x[j])*(y[i]-y[j])>0 else .5 if x[i]==x[j] else 1 for i,j in pairs) if pairs else None
            out.update(mae=st.mean(abs(a-b) for a,b in zip(x,y)),pearson=corr(x,y),spearman=corr(ranks(x),ranks(y)),
                pairwise_rank_loss=loss,top_choice_regret=max(y)-st.mean(y[i] for i in range(len(x)) if x[i]==max(x)))
            intervals=[r for r in valid if 'lower' in r['predictions'][t]]
            out['interval_n']=len(intervals)
            if intervals:out['interval_coverage']=st.mean(r['predictions'][t]['lower']<=r['targets'][t]<=r['predictions'][t]['upper'] for r in intervals)
        if meta['status']=='primary_archival' and relevant:
            # Missing predictions incur loss; cannot improve objective by dropping weak targets.
            err=sum(abs(r['predictions'][t]['score']-r['targets'][t])/meta['scale'] for r in valid)
            objective.append((err+len(relevant)-len(valid))/len(relevant)+.1*(out.get('pairwise_rank_loss') or 0))
        result[t]=out
    return {'targets':result,'development_objective':st.mean(objective) if objective else None}


def cross_validate(panel, tasks, predictor):
    module=load_predictor(predictor);rows=[]
    for f in sorted({r['family'] for r in panel['models']}):
        train=[r for r in panel['models'] if r['family']!=f]
        test=[r for r in panel['models'] if r['family']==f and r['complete_source_coverage']]
        if not test:continue
        preds=predict(module,training(train,tasks),[measurement(r,tasks) for r in test],panel['targets'])
        rows.extend({'id':r['id'],'family':f,'targets':r['targets'],'predictions':p} for r,p in zip(test,preds))
    return summarize(rows,panel['targets'])|{'rows':rows,'note':'Development family CV guides research; adaptively reused and not final hidden evidence.'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--panel');p.add_argument('--submission',default='submission');p.add_argument('--payload');p.add_argument('--output',required=True)
    args=p.parse_args();folder=Path(args.submission)
    if args.payload:
        payload=json.loads(Path(args.payload).read_text());module=load_predictor(folder/'predictor.py')
        result=predict(module,payload['training'],payload['observations'],payload['targets'])
    else:
        panel=json.loads(Path(args.panel or 'development.json').read_text())
        tasks,cost=selection(panel,json.loads((folder/'selection.json').read_text()))
        result=cross_validate(panel,tasks,folder/'predictor.py')|{'selected_tasks':tasks,'development_cost_fraction':cost}
    Path(args.output).write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')


if __name__=='__main__':main()
