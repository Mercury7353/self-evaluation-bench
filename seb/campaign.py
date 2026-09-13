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
    rows = []
    def add(kind, researcher, domain, development, designer):
        row = {'id': f'{kind}--{researcher}--{domain}--b{development:g}',
               'kind': kind, 'researcher': researcher, 'domain': domain,
               'development_usd': development, 'researcher_usd': designer,
               'evaluation_usd': test_count * base['candidate_suite_usd'],
               'candidate_suite_usd': base['candidate_suite_usd'],
               'research_seconds': base['research_seconds']}
        rows.append(row)
    for researcher in ids:
        for domain in domains:
            add('main', researcher, domain['id'], base['development_usd'], base['researcher_usd'])
    for researcher in manifest['budget_ablation']['researchers']:
        if researcher not in ids:
            raise ValueError('Ablation researcher must have a main condition')
        for domain in domains:
            for budget in manifest['budget_ablation']['additional_development_usd']:
                if budget == base['development_usd']:
                    raise ValueError('Middle budget reuses main; do not allocate it twice')
                add('budget', researcher, domain['id'], budget, base['researcher_usd'])
    for baseline in manifest['baselines']:
        for domain in domains:
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
            row = db.execute('SELECT * FROM episodes WHERE id=?', (episode_id,)).fetchone()
            if not row:
                raise ValueError('Unknown episode')
            if row['status'] != 'planned':
                raise ValueError('Already claimed; inspect the existing handle instead of relaunching')
            db.execute("UPDATE episodes SET status='claimed',handle=?,claimed_at=? WHERE id=?",
                       (handle, time.time(), episode_id))
            return dict(row) | {'handle': handle, 'status': 'claimed'}

    def inventory(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM episodes ORDER BY id')]
