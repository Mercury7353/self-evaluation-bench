"""Measure a fixed baseline with the joint contract, without a researcher."""
import hashlib
import json
from pathlib import Path
import signal
import sqlite3
import tempfile
import time

from .cli import doctor, mock_provider, write
from .domain_scoring import score_domain
from .evaluation import load_manifest
from .experiment import accounting, build_gateway, joint_domains, metadata, prepare_workspace
from .experiment_config import load
from .runner import digest_tree
from .supervisor import freeze_program, run_jobs, start_gateway, stop_gateway


def reuse_development_ledger(output, source, cap):
    """Bind one existing wallet to one output, preserving every call and scope."""
    output=Path(output).resolve();source=Path(source).resolve(strict=True)
    destination=output/'gateway/ledger.sqlite'
    if not source.is_file() or source==destination:
        raise ValueError('Expected a distinct existing development ledger')
    destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('The run already has a ledger; inspect it instead of replacing it')
    linked=False
    db=sqlite3.connect(source.as_uri()+'?mode=rw',uri=True,timeout=60)
    try:
        db.execute('BEGIN IMMEDIATE')
        wallets=dict(db.execute('SELECT name,cap FROM wallets').fetchall())
        if wallets!={'development':cap}:
            raise ValueError('Reuse requires exactly the original development wallet and unchanged cap')
        db.execute('CREATE TABLE IF NOT EXISTS development_reuse_owner(id INTEGER PRIMARY KEY CHECK(id=1), output TEXT NOT NULL)')
        owner=db.execute('SELECT output FROM development_reuse_owner WHERE id=1').fetchone()
        if owner:
            raise ValueError('Development wallet is already assigned; inspect its existing output: '+owner[0])
        calls=db.execute('SELECT * FROM calls ORDER BY id').fetchall()
        call_columns=[row[1] for row in db.execute('PRAGMA table_info(calls)')]
        records=[dict(zip(call_columns,row)) for row in calls]
        if any(row['wallet']!='development' for row in records):
            raise ValueError('Existing calls refer to another wallet')
        snapshot={'source':str(source),'output':str(output),'wallet':'development','cap':cap,
            'calls':len(records),'call_ids':[r['id'] for r in records],
            'charged_usd':sum(r['charged'] for r in records if r['charged'] is not None),
            'outstanding_reserved_usd':sum(r['reserve'] for r in records if r['charged'] is None),
            'calls_sha256':hashlib.sha256(json.dumps(records,sort_keys=True,allow_nan=False).encode()).hexdigest(),
            'bound_at':time.time(),
            'price_note':'Inherited charges and reservations are retained. Old usage is not repriced using this run configuration.'}
        db.execute('INSERT INTO development_reuse_owner VALUES(1,?)',(str(output),))
        destination.symlink_to(source);linked=True
        db.commit()
    except BaseException:
        db.rollback()
        if linked:destination.unlink()
        raise
    finally:
        db.close()
    write(output/'ledger-reuse.json',snapshot)
    return snapshot


def run(config_path, submission, output, *, name='fixed', mock=False,
        reuse_ledger=None, expected_config_sha256=None):
    cfg=load(config_path)
    if expected_config_sha256 and cfg['_config_sha256']!=expected_config_sha256:
        raise ValueError('Execution configuration changed after campaign claim')
    if not cfg.get('domain_protocol'):
        raise ValueError('Fixed baseline acceptance requires a domain or joint protocol')
    if name not in ('random','stratified','fixed'):
        raise ValueError('Unknown fixed baseline kind')
    if cfg['evaluation']['preflight']:
        raise ValueError('Run transport calibration separately; a fixed baseline does not repeat it')
    if not mock and any('REPLACE' in m['model'] or 'YOUR_' in cfg['providers'][m['provider']]['upstream'] for m in cfg['models']):
        raise ValueError('Replace candidate and provider placeholders before a paid baseline run')
    source=Path(submission).resolve()
    load_manifest(source,cfg['design']['minimum_items'],domains=joint_domains(cfg))
    if not (source/'predictor.py').is_file():
        raise ValueError('Baseline requires a frozen visible-target predictor')
    doctor(cfg['runtime']['rootfs'])
    # These fields describe this execution, not a new researcher allocation.
    cfg['researchers']=[];cfg['budgets']['researcher_usd']=0
    if cfg['budgets'].get('judge_usd'):
        raise ValueError('Baseline judge calls must count within the declared candidate budgets')
    out=Path(output).resolve();out.mkdir(parents=True,exist_ok=False);out.chmod(0o700)
    process=None;server=None
    state={'phase':'preparing','started':time.time(),'baseline':name,'researcher':None,'mock':mock}
    def update(**values):state.update(values);write(out/'state.json',state)
    def interrupted(signum,frame):raise InterruptedError('Inspect existing baseline jobs and ledger before resuming')
    prior=signal.signal(signal.SIGTERM,interrupted)
    try:
        write(out/'config.resolved.json',{k:v for k,v in cfg.items() if not k.startswith('_')})
        write(out/'provenance.json',{'execution_kind':'fixed_baseline','config_sha256':cfg['_config_sha256'],
            'reference_sha256':cfg['_reference_hashes'],
            'code_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')},
            'started':state['started'],'mock':mock})
        frozen=out/'baseline-frozen';freeze_program(source,frozen)
        write(out/'freeze.json',{'policy':'fixed_before_development','files':digest_tree(frozen),
            'frozen_at':time.time(),'predictor':'submitted','baseline':name})
        if reuse_ledger:reuse_development_ledger(out,reuse_ledger,cfg['budgets']['development_usd'])
        if mock:server=mock_provider()
        with tempfile.TemporaryDirectory(prefix='seb-baseline-') as sockets:
            config,tokens=build_gateway(cfg,None,out,Path(sockets)/'gateway.sock',
                mock_url=f'http://127.0.0.1:{server.server_port}' if server else None)
            work=prepare_workspace(cfg,config,tokens,out)
            freeze_program(frozen,work/'submission')
            acceptance=out/'acceptance-input';acceptance.mkdir()
            freeze_program(frozen,acceptance/'suite')
            process=start_gateway(config,out)
            update(phase='development_evaluation')
            development=run_jobs(config,tokens['development'],[m['id'] for m in cfg['models'] if m['split']=='development'],
                'submission',out/'development-1',config['research_deadline_epoch'])
            (work/'access.json').unlink(missing_ok=True)
            update(phase='acceptance')
            heldout=[m for m in cfg['models'] if m['split']=='holdout']
            accepted=run_jobs(config,tokens['evaluation'],[m['id'] for m in heldout],
                'suite',out/'acceptance-jobs',time.time()+cfg['evaluation']['seconds'])
            # Settle completed calls and retain pending/unknown charges before scoring.
            stop_gateway(process);process=None
            update(phase='scoring')
            report=score_domain(config,acceptance/'suite',development,accepted,cfg['models'],
                cfg['_references'],metadata(cfg,'whitebox'),metadata(cfg,'blackbox'),out/'domain',
                minimum_models=cfg['domain_protocol']['minimum_models'],
                minimum_families=cfg['domain_protocol']['minimum_families'],domains=joint_domains(cfg))
            bill=accounting(out,cfg)
            complete=sum(r.get('score_status')=='valid' for r in accepted)
            result={'baseline':name,'researcher':None,'researcher_calls':0,
                'research_unit':'joint' if joint_domains(cfg) else 'domain',
                'visible_utility':report['visible_utility'],'sealed_utility':report['sealed_utility'],
                'complete_models':complete,'expected_models':len(heldout),'accounting':bill,
                'eligible':complete==len(heldout) and report['visible_utility'] is not None and
                    report['sealed_utility'] is not None and bill['within_budget'] and bill['unknown_calls']==0,
                'mock':mock,**({'paid_api_calls':0,'quality_claim':'none; simulated pipeline fixture'} if mock else {})}
            if joint_domains(cfg):result['domains']=report['domains']
            write(out/'result.json',result)
            update(phase='completed' if result['eligible'] else 'incomplete',finished=time.time())
            return result
    except BaseException as error:
        update(phase='failed',error=type(error).__name__+': '+str(error),finished=time.time())
        raise
    finally:
        stop_gateway(process)
        if server:server.shutdown();server.server_close()
        for path in out.glob('*.secret'):path.unlink()
        (out/'gateway.private.json').unlink(missing_ok=True)
        (out/'researcher-work/access.json').unlink(missing_ok=True)
        signal.signal(signal.SIGTERM,prior)
