"""Durable, atomic USD reservations. Unknown/aborted charges stay reserved."""
import json
import math
import sqlite3
import time
from pathlib import Path

class BudgetExceeded(Exception):
    pass

class Ledger:
    def __init__(self, path):
        # All aliases of a shared ledger must use the same SQLite journal path.
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS wallets(name TEXT PRIMARY KEY, cap REAL);
            CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, wallet TEXT,
              model TEXT, reserve REAL, charged REAL, state TEXT, usage TEXT,
              created REAL, finished REAL);
            CREATE TABLE IF NOT EXISTS budget_scopes(id TEXT PRIMARY KEY, wallet TEXT, cap REAL);
            CREATE TABLE IF NOT EXISTS call_budget_scopes(call_id TEXT, scope TEXT,
              PRIMARY KEY(call_id, scope));
            CREATE TABLE IF NOT EXISTS cache_replays(call_id TEXT PRIMARY KEY,
              cache_key TEXT NOT NULL, source_call_id TEXT NOT NULL,
              source_ledger TEXT NOT NULL, response_sha256 TEXT NOT NULL);
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=60)
        db.row_factory = sqlite3.Row
        return db

    def wallet(self, name, cap):
        with self.connect() as db:
            existing = db.execute('SELECT cap FROM wallets WHERE name=?', (name,)).fetchone()
            if existing and existing['cap'] != cap:
                raise ValueError('Cannot silently change an existing wallet cap')
            db.execute('INSERT OR IGNORE INTO wallets VALUES (?,?)', (name, cap))

    def reserve(self, call_id, wallet, model, amount, *, scopes=None):
        if not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
            raise ValueError('Invalid reservation amount')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT cap FROM wallets WHERE name=?', (wallet,)).fetchone()
            if not row:
                raise ValueError('Unknown wallet')
            used = db.execute('SELECT COALESCE(SUM(COALESCE(charged,reserve)),0) FROM calls WHERE wallet=?', (wallet,)).fetchone()[0]
            if row['cap'] is not None and used + amount > row['cap'] + 1e-9:
                raise BudgetExceeded(f'Wallet {wallet}: available ${row["cap"] - used:.6f}, reservation ${amount:.6f}')
            for scope, cap in (scopes or {}).items():
                if not isinstance(cap, (int, float)) or not math.isfinite(cap) or cap <= 0:
                    raise ValueError('Invalid scope budget')
                prior = db.execute('SELECT wallet,cap FROM budget_scopes WHERE id=?', (scope,)).fetchone()
                if prior and (prior['wallet'] != wallet or prior['cap'] != cap):
                    raise ValueError('Cannot change an existing scope budget')
                db.execute('INSERT OR IGNORE INTO budget_scopes VALUES(?,?,?)', (scope,wallet,cap))
                spent = db.execute('''SELECT COALESCE(SUM(COALESCE(c.charged,c.reserve)),0)
                    FROM calls c JOIN call_budget_scopes s ON c.id=s.call_id WHERE s.scope=?''', (scope,)).fetchone()[0]
                if spent + amount > cap + 1e-9:
                    error = BudgetExceeded(f'Scope budget exhausted: available ${cap-spent:.6f}, reservation ${amount:.6f}')
                    error.scope = scope
                    raise error
            db.execute('INSERT INTO calls VALUES(?,?,?,?,NULL,?,NULL,?,NULL)',
                       (call_id, wallet, model, amount, 'reserved', time.time()))
            db.executemany('INSERT INTO call_budget_scopes VALUES(?,?)', [(call_id,s) for s in (scopes or {})])

    def finish(self, call_id, charge, usage, state):
        with self.connect() as db:
            db.execute('UPDATE calls SET charged=?,usage=?,state=?,finished=? WHERE id=?',
                       (charge, json.dumps(usage), state, time.time(), call_id))

    def finish_cached(self, call_id, charge, usage, record):
        """Settle equivalent quota and zero-provider-cost provenance atomically."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state,reserve FROM calls WHERE id=?', (call_id,)).fetchone()
            if not row or row['state'] != 'reserved' or not 0 <= charge <= row['reserve'] + 1e-9:
                raise ValueError('Cached settlement requires a fresh adequate reservation')
            db.execute('UPDATE calls SET charged=?,usage=?,state=?,finished=? WHERE id=?',
                (charge, json.dumps(usage), 'completed', time.time(), call_id))
            db.execute('INSERT INTO cache_replays VALUES(?,?,?,?,?)', (call_id, record['key'],
                record['source_call_id'], record['source_ledger'], record['response_sha256']))

    def status(self, wallet=None):
        with self.connect() as db:
            wallets = db.execute('SELECT * FROM wallets' + (' WHERE name=?' if wallet else ''), (wallet,) if wallet else ()).fetchall()
            out = []
            for w in wallets:
                row = db.execute('SELECT COUNT(*) AS calls, COALESCE(SUM(charged),0) AS charged, COALESCE(SUM(CASE WHEN charged IS NULL THEN reserve ELSE 0 END),0) AS outstanding FROM calls WHERE wallet=?', (w['name'],)).fetchone()
                cached = db.execute('SELECT COUNT(*) AS hits, COALESCE(SUM(c.charged),0) AS equivalent FROM calls c JOIN cache_replays r ON c.id=r.call_id WHERE c.wallet=?', (w['name'],)).fetchone()
                out.append(dict(w) | dict(row) | {'equivalent_charge_usd':row['charged'],
                    'provider_metered_usd':row['charged']-cached['equivalent'],
                    'response_cache_hits':cached['hits']})
            return out
