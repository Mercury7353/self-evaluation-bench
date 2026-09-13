"""Immutable experiment matrix and allocation registry, independent of live jobs.

The registry never infers a stopped process from a stale timestamp. A claimed run
cannot be claimed again: operators must inspect its existing process/artifacts.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import time


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def episodes(manifest):
    researchers = manifest['researchers']
    domains = manifest['domains']
    base = manifest['budgets']
    ids = [r['id'] for r in researchers]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate researchers')
    candidates = manifest['candidates']
    mids = [m['id'] for m in candidates]
    if len(mids) != len(set(mids)):
        raise ValueError('Duplicate candidates')
    if any(m['split'] not in ('development', 'holdout') for m in candidates):
        raise ValueError('Invalid candidate split')
    if len(domains) != len({d['id'] for d in domains}):
        raise ValueError('Duplicate domains')
    targets = [t['id'] for d in domains for t in d['targets']]
    if len(targets) != len(set(targets)):
        raise ValueError('Target IDs must be unique across domains')
    for domain in domains:
        if {t['visibility'] for t in domain['targets']} != {'visible', 'sealed'}:
            raise ValueError('Each domain needs visible and sealed targets')
    test_count = sum(m['split'] == 'holdout' for m in candidates)
    if not test_count:
        raise ValueError('No held-out candidates')
    unit = manifest.get('research_unit', 'per_domain')
    if unit not in ('per_domain', 'joint'):
        raise ValueError('Research unit must be per_domain or joint')
    groups = [{'id': 'joint'}] if unit == 'joint' else domains
    rows = []
    def add(kind, researcher, domain, development, designer):
        row = {'id': f'{kind}--{researcher}--{domain}--b{development:g}',
               'kind': kind, 'researcher': researcher, 'domain': domain,
               'development_usd': development, 'researcher_usd': designer,
               'evaluation_usd': test_count * base['candidate_suite_usd'],
               'candidate_suite_usd': base['candidate_suite_usd'],
               'research_seconds': base['research_seconds']}
        if unit == 'joint':
            row['domains'] = [d['id'] for d in domains]
        rows.append(row)
    for researcher in ids:
        for domain in groups:
            add('main', researcher, domain['id'], base['development_usd'], base['researcher_usd'])
    for researcher in manifest['budget_ablation']['researchers']:
        if researcher not in ids:
            raise ValueError('Ablation researcher must have a main condition')
        for domain in groups:
            for budget in manifest['budget_ablation']['additional_development_usd']:
                if budget == base['development_usd']:
                    raise ValueError('Middle budget reuses main; do not allocate it twice')
                add('budget', researcher, domain['id'], budget, base['researcher_usd'])
    for baseline in manifest['baselines']:
        for domain in groups:
            add('baseline', baseline, domain['id'], base['development_usd'], 0)
    if len(rows) != len({r['id'] for r in rows}):
        raise ValueError('Duplicate episode allocation')
    for row in rows:
        for field in ('development_usd', 'researcher_usd', 'evaluation_usd'):
            value = row[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < float('inf'):
                raise ValueError('Invalid budget')
    total = sum(r[k] for r in rows for k in ('development_usd', 'researcher_usd', 'evaluation_usd'))
    if total > manifest['run_allocation_ceiling_usd'] + 1e-9:
        raise ValueError('Episode allocations exceed campaign ceiling')
    return rows


class Registry:
    def __init__(self, root, manifest):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'campaign.sqlite'
        rows = episodes(manifest)
        encoded = canonical(manifest)
        digest = hashlib.sha256(encoded).hexdigest()
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS episodes(
                id TEXT PRIMARY KEY, specification TEXT NOT NULL, status TEXT NOT NULL,
                artifact_path TEXT NOT NULL, handle TEXT, claimed_at REAL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute("SELECT value FROM metadata WHERE key='manifest_sha256'").fetchone()
            if prior and prior[0] != digest:
                raise ValueError('Campaign manifest is immutable; no silent budget or panel change')
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('manifest_sha256',?)", (digest,))
            for row in rows:
                db.execute('INSERT OR IGNORE INTO episodes VALUES (?,?,?,?,NULL,NULL)',
                           (row['id'], canonical(row).decode(), 'planned', str(self.root / 'runs' / row['id'])))
        snapshot = self.root / 'manifest.frozen.json'
        if snapshot.exists() and snapshot.read_bytes() != encoded:
            raise ValueError('Manifest snapshot changed')
        if not snapshot.exists():
            snapshot.write_bytes(encoded)
        self.digest = digest

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def claim(self, episode_id, handle):
        if not handle:
            raise ValueError('A concrete supervisor handle is required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            suspended = db.execute("SELECT value FROM metadata WHERE key='launch_suspended'").fetchone()
            if suspended:
                raise ValueError('Campaign launch suspended: ' + suspended[0])
            row = db.execute('SELECT * FROM episodes WHERE id=?', (episode_id,)).fetchone()
            if not row:
                raise ValueError('Unknown episode')
            if row['status'] != 'planned':
                raise ValueError('Already claimed; inspect the existing handle instead of relaunching')
            db.execute("UPDATE episodes SET status='claimed',handle=?,claimed_at=? WHERE id=?",
                       (handle, time.time(), episode_id))
            return dict(row) | {'handle': handle, 'status': 'claimed'}

    def suspend_launches(self, reason):
        """Prevent further claims without modifying allocations, claims or ledgers."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('A suspension reason is required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('launch_suspended',?)", (reason,))

    def inventory(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM episodes ORDER BY id')]


def run_baseline_episode(root, episode_id, config_path, submission, handle, *, mock=False):
    """Claim the declared baseline and apply its existing-wallet allocation."""
    from .baseline import run
    from .experiment_config import load
    manifest=json.loads((Path(root)/'manifest.frozen.json').read_text())
    registry=Registry(root,manifest)
    selected=[row for row in episodes(manifest) if row['id']==episode_id]
    if len(selected)!=1 or selected[0]['kind']!='baseline' or manifest.get('research_unit')!='joint':
        raise ValueError('Select one registered joint baseline episode')
    allocation=selected[0];cfg=load(config_path)
    for key,budget in [('development_usd','development_usd'),('evaluation_usd','evaluation_usd'),('candidate_suite_usd','suite_usd')]:
        if allocation[key]!=cfg['budgets'][budget]:
            raise ValueError('Execution budget does not match the registered allocation: '+budget)
    if allocation['researcher_usd']!=0 or cfg['design']['seconds']!=allocation['research_seconds']:
        raise ValueError('Baseline researcher/time allocation mismatch')
    if cfg.get('domain_protocol',{}).get('version')!=2 or set(cfg['domain_protocol']['domains'])!=set(allocation['domains']):
        raise ValueError('Execution must include all registered joint domains')
    def panel(candidates):
        return {m['id']:(m['model'],m['split'],m['family']) for m in candidates}
    if panel(cfg['models'])!=panel(manifest['candidates']):
        raise ValueError('Execution candidate panel differs from campaign allocation')
    expected={(t['id'],t['visibility'],d['id']) for d in manifest['domains'] for t in d['targets']}
    actual={(t['id'],visibility,t['domain']) for visibility,key in [('visible','whitebox'),('sealed','blackbox')]
            for t in cfg['benchmarks'][key]}
    if actual!=expected:raise ValueError('Execution targets differ from campaign allocation')
    reuse=None
    preflight=manifest.get('preflight_allocation',{})
    if episode_id==preflight.get('episode_id'):
        if preflight.get('wallet')!='development' or preflight.get('counts_within_development_usd')!=allocation['development_usd']:
            raise ValueError('Invalid existing probe-wallet allocation')
        reuse=preflight.get('reuse_existing_ledger')
        if not reuse or not Path(reuse).is_file():
            raise ValueError('The allocated original development ledger must exist')
    claimed=registry.claim(episode_id,handle)
    # A failed launch remains claimed. Inspect the concrete handle/artifacts;
    # do not use failure as permission to mint another wallet or execution.
    return run(config_path,submission,claimed['artifact_path'],name=allocation['researcher'],
        mock=mock,reuse_ledger=reuse,expected_config_sha256=cfg['_config_sha256'])
