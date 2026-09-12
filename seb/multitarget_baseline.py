"""Simple source-pool means + ridge calibration, supplied as a baseline, not a required design."""
import numpy as np


def features(obs,pools):
    return [np.mean([v for t,v in obs.items() if t.split('/')[0]==p and v is not None])
            if any(t.split('/')[0]==p and v is not None for t,v in obs.items()) else float('nan') for p in pools]


def fit(training_rows,target_metadata):
    pools=sorted({t.split('/')[0] for r in training_rows for t in r['observations']})
    x=np.array([features(r['observations'],pools) for r in training_rows])
    means=np.array([np.mean(c[np.isfinite(c)]) if np.any(np.isfinite(c)) else .5 for c in x.T])
    x=np.where(np.isfinite(x),x,means);x=np.column_stack([np.ones(len(x)),x]);models={}
    for t,meta in target_metadata.items():
        ids=[i for i,r in enumerate(training_rows) if t in r['targets']]
        if not ids:continue
        a=x[ids];y=np.array([training_rows[i]['targets'][t]/meta['scale'] for i in ids])
        penalty=np.eye(a.shape[1])*.1;penalty[0,0]=1e-8
        b=np.linalg.solve(a.T@a+penalty,a.T@y)
        models[t]={'coef':b,'scale':meta['scale'],'residual':max(.03,float(np.sqrt(np.mean((a@b-y)**2))))}
    return {'pools':pools,'means':means,'models':models}


def predict(fitted,observations):
    a=np.array(features(observations,fitted['pools']));a=np.where(np.isfinite(a),a,fitted['means']);a=np.r_[1,a];out={}
    for t,m in fitted['models'].items():
        v=float(a@m['coef']);v=min(1,max(0,v)) if m['scale']==1 else v
        out[t]={'score':v*m['scale'],'lower':(max(0,v-2*m['residual']) if m['scale']==1 else v-2*m['residual'])*m['scale'],
                'upper':(min(1,v+2*m['residual']) if m['scale']==1 else v+2*m['residual'])*m['scale']}
    return out
