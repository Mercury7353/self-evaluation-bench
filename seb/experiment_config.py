"""YAML experiment configuration; private target data never enters the researcher view."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sysconfig

import yaml
from .execution_policy import DEFAULT_POLICY,policy_for


def positive(value,name):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
        raise ValueError(name+' must be finite and positive')
    return value


def identifier(value,name):
    if not isinstance(value,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}',value):
        raise ValueError(name+' must be a simple identifier')
    return value


def load(path, *, resolve_inputs=True):
    path=Path(path).resolve();cfg=yaml.safe_load(path.read_text())
    if not isinstance(cfg,dict) or cfg.get('version')!=1:raise ValueError('YAML version: 1 required')
    allowed={'version','name','runtime','providers','researchers','models','benchmarks','budgets','design','evaluation','overall'}
    unknown=set(cfg)-allowed
    if unknown:raise ValueError('Unknown YAML fields: '+', '.join(sorted(unknown)))
    cfg=copy.deepcopy(cfg);cfg['name']=identifier(cfg.get('name'),'name')
    def local(value):
        expanded=os.path.expandvars(os.path.expanduser(value))
        if '$' in expanded:raise ValueError('Unresolved environment variable in path: '+value)
        return str((path.parent/Path(expanded)).resolve())
    runtime=cfg.setdefault('runtime',{})
    if not runtime.get('rootfs'):raise ValueError('runtime.rootfs required')
    runtime['rootfs']=local(runtime['rootfs'])
    runtime['science_packages']=local(runtime.get('science_packages',sysconfig.get_path('purelib')))
    if resolve_inputs:
        if not (Path(runtime['rootfs'])/'usr/local/bin/python').is_file():raise ValueError('runtime.rootfs lacks /usr/local/bin/python')
        if not Path(runtime['science_packages']).is_dir():raise ValueError('Missing runtime.science_packages')
    providers=cfg.get('providers',{})
    for pid,provider in providers.items():
        identifier(pid,'provider id')
        if not provider.get('upstream') or not provider.get('key_env'):raise ValueError('Each provider requires upstream and key_env')
    models=cfg.get('models',[]);researchers=cfg.get('researchers',[])
    if not models or not researchers:raise ValueError('Nonempty models and researchers lists required')
    for group,label in [(models,'model'),(researchers,'researcher')]:
        ids=[]
        for item in group:
            ids.append(identifier(item.get('id'),label+' id'))
            if item.get('provider') not in providers:raise ValueError('Unknown provider for '+item['id'])
            if not item.get('model'):raise ValueError('Provider model name required')
            for price in ['input','output']:
                value=item.get('price',{}).get(price)
                if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:raise ValueError('Finite nonnegative per-million-token prices required')
            for key in ['cache_read_multiplier','cache_write_multiplier','reservation_output_headroom']:
                if key in item['price']:
                    v=item['price'][key]
                    if type(v) not in (float,int) or not math.isfinite(v) or v<0:raise ValueError('Invalid price multiplier')
                    if key=='cache_read_multiplier' and v>1:raise ValueError('cache_read_multiplier must be in [0,1]')
        if len(ids)!=len(set(ids)):raise ValueError('Duplicate '+label+' IDs')
    if set(m['id'] for m in models) & set(r['id'] for r in researchers):raise ValueError('Researcher and candidate IDs must be distinct')
    if len({m['family'] for m in models if m['split']=='development'})<2:raise ValueError('At least two development model families required')
    if {m['family'] for m in models if m['split']=='development'} & {m['family'] for m in models if m['split']=='holdout'}:raise ValueError('Holdout families must be disjoint from development families')
    for model in models:
        identifier(model.get('family'),'model family')
        if model.get('split') not in ['development','holdout']:raise ValueError('Model split must be development or holdout')
    if len([m for m in models if m['split']=='development'])<3:raise ValueError('At least three development models required')
    for researcher in researchers:
        if researcher.get('prompt_file') and resolve_inputs and not Path(local(researcher['prompt_file'])).is_file():raise ValueError('Missing researcher prompt_file')
        if researcher.get('harness') not in ['claude_code','mock']:raise ValueError('Supported researcher harnesses: claude_code, mock')
        if researcher.get('prompt_file'):researcher['prompt_file']=local(researcher['prompt_file'])
    budgets=cfg.get('budgets',{})
    for name in ['researcher_usd','development_usd','evaluation_usd','suite_usd','item_usd']:positive(budgets.get(name),'budgets.'+name)
    design=cfg.setdefault('design',{});design.setdefault('rounds',1);design.setdefault('seconds',3600);design.setdefault('minimum_items',100)
    for k in ['rounds','seconds','minimum_items']:
        if type(design[k]) is not int or design[k]<1:raise ValueError('design.'+k+' must be a positive integer')
    evaluation=cfg.setdefault('evaluation',{});evaluation.setdefault('seconds',7200);evaluation.setdefault('model_concurrency',2);evaluation.setdefault('requests_per_model',2)
    for k in ['seconds','model_concurrency','requests_per_model']:
        if type(evaluation[k]) is not int or evaluation[k]<1:raise ValueError('evaluation.'+k+' must be a positive integer')
    evaluation['policy']=DEFAULT_POLICY | evaluation.get('policy',{});policy_for({'evaluation_policy':evaluation['policy']})
    overall=cfg.setdefault('overall',{});overall.setdefault('metric','macro_spearman');overall.setdefault('source','family_cv');overall.setdefault('minimum_models',3);overall.setdefault('constant_prediction','zero')
    if overall['metric']!='macro_spearman' or overall['source'] not in ['family_cv','raw_mean']:raise ValueError('overall uses macro_spearman with source family_cv or raw_mean')
    if type(overall['minimum_models']) is not int or overall['minimum_models']<3:raise ValueError('overall.minimum_models must be at least 3')
    if overall['constant_prediction'] not in ['zero','undefined']:raise ValueError('constant_prediction must be zero or undefined')
    targets=cfg.get('benchmarks',{});seen=set();cfg['_references']={};cfg['_reference_hashes']={}
    if set(targets)-{'whitebox','blackbox'}:raise ValueError('benchmarks supports whitebox and blackbox lists')
    if not targets.get('whitebox') or not targets.get('blackbox'):raise ValueError('Both whitebox and blackbox targets are required')
    for visibility in ['whitebox','blackbox']:
        for target in targets[visibility]:
            tid=identifier(target.get('id'),'benchmark id')
            if tid in seen:raise ValueError('Duplicate benchmark id')
            seen.add(tid);positive(target.setdefault('scale',1),'benchmark.scale')
            target['reference']=local(target['reference'])
            target['resources']=[local(x) for x in target.get('resources',[])]
            if visibility=='blackbox' and target['resources']:raise ValueError('Black-box resources must remain outside the researcher workspace')
            if resolve_inputs:
                refs=json.loads(Path(target['reference']).read_text())
                if not isinstance(refs,dict):raise ValueError('Each reference JSON maps candidate IDs to numeric scores')
                if set(refs)-{m['id'] for m in models}:raise ValueError('Reference contains unknown candidate IDs')
                if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in refs.values()):raise ValueError('Reference scores must be finite')
                cfg['_references'][tid]=refs;cfg['_reference_hashes'][tid]=hashlib.sha256(Path(target['reference']).read_bytes()).hexdigest()
                for resource in target['resources']:
                    if not Path(resource).exists():raise ValueError('Missing white-box resource: '+resource)
    cfg['_config_path']=str(path);cfg['_config_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    return cfg


def researcher_view(cfg):
    development={m['id'] for m in cfg['models'] if m['split']=='development'}
    return {'models':sorted(development),'targets':{
        t['id']:{'scale':t['scale'],'scores':{m:s for m,s in cfg['_references'][t['id']].items() if m in development}}
        for t in cfg['benchmarks']['whitebox']},
        'budget_usd':cfg['budgets']['development_usd'],'minimum_items':cfg['design']['minimum_items']}


def describe(cfg):
    return {'name':cfg['name'],'researchers':[{'id':r['id'],'harness':r['harness'],'model':r['model']} for r in cfg['researchers']],
            'development_models':[m['id'] for m in cfg['models'] if m['split']=='development'],
            'holdout_models':[m['id'] for m in cfg['models'] if m['split']=='holdout'],
            'whitebox':[t['id'] for t in cfg['benchmarks']['whitebox']], 'blackbox':[t['id'] for t in cfg['benchmarks']['blackbox']],
            'budgets_per_researcher_run':cfg['budgets'],'overall':cfg['overall'],'paid_api_calls':0}
