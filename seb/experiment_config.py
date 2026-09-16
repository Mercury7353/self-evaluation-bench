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
    allowed={'version','name','runtime','providers','researchers','models','auxiliary_models','benchmarks','budgets','design','evaluation','overall','domain_protocol','response_cache'}
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
    if runtime.get('image_cache'):runtime['image_cache']=local(runtime['image_cache'])
    if type(runtime.get('verifier_network',False)) is not bool:raise ValueError('runtime.verifier_network must be boolean')
    if runtime.get('codex_binary'):
        runtime['codex_binary']=local(runtime['codex_binary'])
    if resolve_inputs:
        if not (Path(runtime['rootfs'])/'usr/local/bin/python').is_file():raise ValueError('runtime.rootfs lacks /usr/local/bin/python')
        if not Path(runtime['science_packages']).is_dir():raise ValueError('Missing runtime.science_packages')
    providers=cfg.get('providers',{})
    for pid,provider in providers.items():
        identifier(pid,'provider id')
        if not provider.get('upstream') or not provider.get('key_env'):raise ValueError('Each provider requires upstream and key_env')
        if provider.get('wire_api','anthropic') not in ('anthropic','openai_responses','chat_completions'):
            raise ValueError('Provider wire_api must be anthropic, openai_responses or chat_completions')
    models=cfg.get('models',[]);researchers=cfg.get('researchers',[])
    auxiliary=cfg.get('auxiliary_models',[])
    if not isinstance(auxiliary,list):raise ValueError('auxiliary_models must be a list')
    if not models or not researchers:raise ValueError('Nonempty models and researchers lists required')
    for group,label in [(models,'model'),(researchers,'researcher'),(auxiliary,'auxiliary model')]:
        ids=[]
        for item in group:
            if not isinstance(item,dict):raise ValueError(label+' must be a mapping')
            ids.append(identifier(item.get('id'),label+' id'))
            if item.get('availability','ready') not in ('ready','pending'):
                raise ValueError('Invalid model availability')
            if item.get('availability')=='pending':
                if label!='model' or item.get('split')!='holdout' or not cfg.get('domain_protocol'):
                    raise ValueError('Only declared domain holdout candidates may be pending')
                if not isinstance(item.get('pending_reason'),str) or not item['pending_reason'].strip():
                    raise ValueError('Pending candidate requires pending_reason')
                continue
            if item.get('provider') not in providers:raise ValueError('Unknown provider for '+item['id'])
            if not item.get('model'):raise ValueError('Provider model name required')
            if providers[item['provider']].get('wire_api') in ('openai_responses','chat_completions'):
                if not item.get('effort'):raise ValueError('Responses model needs a frozen effort')
                limits=item.get('native_limits',{})
                for name in ('max_context_tokens','max_output_tokens'):
                    if type(limits.get(name)) is not int or limits[name]<=0:raise ValueError('Responses model needs native_limits.'+name)
                if limits['max_output_tokens']>131072:raise ValueError('Unsupported Responses model output limit')
                if providers[item['provider']].get('wire_api')=='chat_completions' and item['effort'] not in ('none','minimal','low','medium','high','xhigh','max'):
                    raise ValueError('Invalid frozen Chat effort')
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
    if {a['id'] for a in auxiliary} & {m['id'] for m in models+researchers}:
        raise ValueError('Auxiliary IDs must be distinct from researcher and candidate IDs')
    for helper in auxiliary:
        roles=helper.get('roles')
        if not isinstance(roles,list) or not roles or any(r not in ('grader','simulator') for r in roles) or len(set(roles))!=len(roles):
            raise ValueError('Auxiliary roles must be a nonempty unique list of grader/simulator')
        if 'split' in helper or 'family' in helper or 'harness' in helper:
            raise ValueError('Auxiliary models are not candidate or researcher panel members')
        if helper['model'] in {m.get('model') for m in models if m.get('split')=='holdout'}:
            raise ValueError('Auxiliary model cannot expose a held-out candidate backend')
    if len({m['family'] for m in models if m['split']=='development'})<2:raise ValueError('At least two development model families required')
    if not cfg.get('domain_protocol') and {m['family'] for m in models if m['split']=='development'} & {m['family'] for m in models if m['split']=='holdout'}:raise ValueError('Holdout families must be disjoint from development families')
    for model in models:
        identifier(model.get('family'),'model family')
        if model.get('split') not in ['development','holdout']:raise ValueError('Model split must be development or holdout')
    if len([m for m in models if m['split']=='development'])<3:raise ValueError('At least three development models required')
    for researcher in researchers:
        if providers[researcher['provider']].get('wire_api')=='chat_completions':
            raise ValueError('Chat adapter is for candidate/auxiliary models; researchers use their native harness provider')
        if researcher.get('prompt_file') and resolve_inputs and not Path(local(researcher['prompt_file'])).is_file():raise ValueError('Missing researcher prompt_file')
        if researcher.get('harness') not in ['claude_code','codex','mock']:raise ValueError('Supported researcher harnesses: claude_code, codex, mock')
        if providers[researcher['provider']].get('wire_api')=='openai_responses' and researcher['harness']=='claude_code':
            raise ValueError('Responses researchers use the native Codex route; the adapter is for candidate wallets')
        if researcher['harness']=='codex':
            if not researcher.get('effort'):raise ValueError('Codex researcher requires a frozen effort')
            limits=researcher.get('native_limits',{})
            if set(limits)-{'max_context_tokens','max_output_tokens','timeout_seconds'}:raise ValueError('Unknown native model limit')
            for key in ('max_context_tokens','max_output_tokens'):
                if type(limits.get(key)) is not int or limits[key]<=0:raise ValueError('Codex researcher requires positive native_limits.'+key)
            if limits['max_output_tokens']>131072:raise ValueError('Unsupported native output limit')
            if 'timeout_seconds' in limits:positive(limits['timeout_seconds'],'native_limits.timeout_seconds')
        if researcher.get('prompt_file'):researcher['prompt_file']=local(researcher['prompt_file'])
    budgets=cfg.get('budgets',{})
    for name in ['researcher_usd','development_usd','evaluation_usd','suite_usd','item_usd']:positive(budgets.get(name),'budgets.'+name)
    design=cfg.setdefault('design',{});design.setdefault('rounds',1);design.setdefault('seconds',3600);design.setdefault('minimum_items',1 if cfg.get('domain_protocol') else 100);design.setdefault('checkpoint_seconds',design['seconds'] if cfg.get('domain_protocol') else min(7200,design['seconds']))
    for k in ['rounds','seconds','minimum_items','checkpoint_seconds']:
        if type(design[k]) is not int or design[k]<1:raise ValueError('design.'+k+' must be a positive integer')
    if 'continuation' in design:
        policy=design['continuation']
        if not isinstance(policy,dict) or set(policy)!={'reprompt_remaining_seconds','transport_retries'}:
            raise ValueError('Invalid design.continuation fields')
        if type(policy['reprompt_remaining_seconds']) is not int or not 1<=policy['reprompt_remaining_seconds']<=design['seconds']:
            raise ValueError('Invalid reprompt threshold')
        if type(policy['transport_retries']) is not int or not 0<=policy['transport_retries']<=5:
            raise ValueError('Invalid transport retry count')
        if any(r['harness']!='codex' for r in researchers):
            raise ValueError('Continuation currently requires the Codex harness')
    if 'domain_protocol' in cfg:
        protocol=cfg['domain_protocol']
        if not isinstance(protocol,dict) or protocol.get('version') not in (1,2,3):
            raise ValueError('domain_protocol.version must be 1, 2 or 3')
        if set(protocol)-{'version','minimum_models','minimum_families','domains','allow_pending_references','score_mode'}:
            raise ValueError('Unknown domain_protocol field')
        if protocol.get('score_mode','predicted_target') not in ('predicted_target','raw_domain'):
            raise ValueError('Invalid domain_protocol.score_mode')
        if protocol.get('score_mode')=='raw_domain' and protocol.get('version') not in (2,3):
            raise ValueError('raw_domain requires a joint domain protocol')
        if type(protocol.get('allow_pending_references',False)) is not bool:
            raise ValueError('domain_protocol.allow_pending_references must be boolean')
        if protocol['version'] in (2,3):
            domains=protocol.get('domains')
            if not isinstance(domains,list) or len(domains)!=3:
                raise ValueError('Joint protocol requires three domain IDs')
            for domain in domains:identifier(domain,'domain id')
            if len(set(domains))!=len(domains):raise ValueError('Duplicate domain IDs')
        elif 'domains' in protocol:
            raise ValueError('Multiple domains require joint protocol version 2')
        protocol.setdefault('minimum_models',8);protocol.setdefault('minimum_families',4)
        for key,minimum in [('minimum_models',3),('minimum_families',2)]:
            if type(protocol[key]) is not int or protocol[key]<minimum:
                raise ValueError('Invalid domain_protocol.'+key)
        if design['rounds']!=1:
            raise ValueError('Domain protocol uses one independent researcher run')
        if design['checkpoint_seconds']!=design['seconds']:
            raise ValueError('Domain researcher receives the full configured continuous time window')
    if 'final_development_measurement' in design and type(design['final_development_measurement']) is not bool:
        raise ValueError('final_development_measurement must be boolean')
    evaluation=cfg.setdefault('evaluation',{});evaluation.setdefault('seconds',7200);evaluation.setdefault('model_concurrency',2);evaluation.setdefault('requests_per_model',2)
    evaluation.setdefault('acceptance_model_concurrency',evaluation['model_concurrency'])
    for k in ['seconds','model_concurrency','requests_per_model','acceptance_model_concurrency']:
        if type(evaluation[k]) is not int or evaluation[k]<1:raise ValueError('evaluation.'+k+' must be a positive integer')
    evaluation.setdefault('preflight',False)
    if type(evaluation['preflight']) is not bool:raise ValueError('evaluation.preflight must be boolean')
    if 'judge_usd' in budgets:positive(budgets['judge_usd'],'budgets.judge_usd')
    evaluation['policy']=DEFAULT_POLICY | evaluation.get('policy',{});policy_for({'evaluation_policy':evaluation['policy']})
    overall=cfg.setdefault('overall',{});overall.setdefault('metric','macro_spearman');overall.setdefault('source','family_cv');overall.setdefault('minimum_models',3);overall.setdefault('constant_prediction','zero')
    if overall['metric']!='macro_spearman' or overall['source'] not in ['family_cv','raw_mean','raw_domain']:raise ValueError('overall uses macro_spearman with source family_cv, raw_mean or raw_domain')
    if cfg.get('domain_protocol',{}).get('score_mode')=='raw_domain':overall['source']='raw_domain'
    elif overall['source']=='raw_domain':raise ValueError('raw_domain source requires raw_domain protocol mode')
    if type(overall['minimum_models']) is not int or overall['minimum_models']<3:raise ValueError('overall.minimum_models must be at least 3')
    if overall['constant_prediction'] not in ['zero','undefined']:raise ValueError('constant_prediction must be zero or undefined')
    targets=cfg.get('benchmarks',{});seen=set();cfg['_references']={};cfg['_reference_hashes']={}
    if set(targets)-{'whitebox','blackbox'}:raise ValueError('benchmarks supports whitebox and blackbox lists')
    if not targets.get('whitebox') or not targets.get('blackbox'):raise ValueError('Both whitebox and blackbox targets are required')
    if cfg.get('domain_protocol',{}).get('version')==1 and any(len(targets[v])!=2 for v in ['whitebox','blackbox']):
        raise ValueError('Domain protocol requires two visible and two sealed targets')
    if cfg.get('domain_protocol',{}).get('version') in (2,3):
        domains=cfg['domain_protocol']['domains']
        for visibility in ['whitebox','blackbox']:
            if any(t.get('domain') not in domains for t in targets[visibility]):
                raise ValueError('Each joint target needs a declared domain')
            if cfg['domain_protocol']['version']==2 and any(sum(t['domain']==d for t in targets[visibility])!=2 for d in domains):
                raise ValueError('Joint protocol requires two visible and two sealed targets per domain')
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
    if cfg.get('domain_protocol'):
        held={m['id']:m['family'] for m in models if m['split']=='holdout'}
        dev={m['id']:m['family'] for m in models if m['split']=='development'}
        pending=cfg['domain_protocol'].get('allow_pending_references',False)
        cfg['_reference_panels']={};cfg['_pending_reference_targets']=[]
        for visibility in ['whitebox','blackbox']:
            for target in targets[visibility]:
                refs=cfg['_references'].get(target['id'],{})
                panel=target.get('holdout_panel')
                if panel is None:
                    if pending:raise ValueError('Pending references require an explicit holdout_panel for '+target['id'])
                    if not resolve_inputs:continue
                    panel=[m for m in held if m in refs]
                if not isinstance(panel,list) or any(not isinstance(m,str) or m not in held for m in panel) or len(panel)!=len(set(panel)):
                    raise ValueError('holdout_panel must contain unique held-out candidate IDs')
                if len(panel)<cfg['domain_protocol']['minimum_models'] or len({held[m] for m in panel})<cfg['domain_protocol']['minimum_families']:
                    raise ValueError('Insufficient frozen holdout reference coverage for '+target['id'])
                cfg['_reference_panels'][target['id']]=list(panel)
                missing=[m for m in panel if m not in refs]
                if resolve_inputs and missing:
                    if not pending:raise ValueError('Insufficient frozen holdout reference coverage for '+target['id'])
                    reason=target.get('reference_pending_reason')
                    if not isinstance(reason,str) or not reason.strip():
                        raise ValueError('Missing reference_pending_reason for '+target['id'])
                    cfg['_pending_reference_targets'].append(target['id'])
                if visibility=='whitebox' and resolve_inputs:
                    development_panel=set(dev)&set(refs)
                    if len(development_panel)<3 or len({dev[m] for m in development_panel})<2:
                        raise ValueError('Insufficient visible development reference coverage for '+target['id'])
    if 'response_cache' in cfg:
        from .response_cache import validate_config
        cache=validate_config(cfg['response_cache'])
        cache['directory']=local(cfg['response_cache']['directory'])
        cfg['response_cache']=validate_config(cache,[runtime['rootfs'],runtime['science_packages'],
            *[r for t in targets['whitebox'] for r in t['resources']]])
    cfg['_config_path']=str(path);cfg['_config_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    return cfg


def auxiliary_view(cfg):
    """Public helper handles and frozen costs, without backend identities or keys."""
    return [{k:copy.deepcopy(m[k]) for k in ('id','roles','price','effort') if k in m}
            for m in cfg.get('auxiliary_models',[])]


def researcher_view(cfg):
    development={m['id'] for m in cfg['models'] if m['split']=='development'}
    return {'models':sorted(development),
        **({'auxiliary_models':auxiliary_view(cfg)} if cfg.get('auxiliary_models') else {}),'targets':{
        t['id']:{'scale':t['scale'],**({'domain':t['domain']} if 'domain' in t else {}),
                 'scores':{m:s for m,s in cfg['_references'][t['id']].items() if m in development}}
        for t in cfg['benchmarks']['whitebox']},
        **({'domains':{d:[t['id'] for t in cfg['benchmarks']['whitebox'] if t['domain']==d]
             for d in cfg['domain_protocol']['domains']}} if cfg.get('domain_protocol',{}).get('version') in (2,3) else {}),
        'budget_usd':cfg['budgets']['development_usd'],'minimum_items':cfg['design']['minimum_items']}


def describe(cfg):
    return {'name':cfg['name'],'researchers':[{'id':r['id'],'harness':r['harness'],'model':r['model']} for r in cfg['researchers']],
            'development_models':[m['id'] for m in cfg['models'] if m['split']=='development'],
            'holdout_models':[m['id'] for m in cfg['models'] if m['split']=='holdout'],
            'auxiliary_models':auxiliary_view(cfg),
            **({'reference_panels':cfg.get('_reference_panels',{}),
                'pending_reference_targets':cfg.get('_pending_reference_targets',[])} if cfg.get('domain_protocol') else {}),
            'whitebox':[t['id'] for t in cfg['benchmarks']['whitebox']], 'blackbox':[t['id'] for t in cfg['benchmarks']['blackbox']],
            'budgets_per_researcher_run':cfg['budgets'],'overall':cfg['overall'],'paid_api_calls':0}
