"""Private, exact-request response reuse; callers still spend equivalent quota.

Only the gateway writes here. Entries contain no credentials. The campaign owner
must freeze the namespace, provider configuration, prices and sample semantics.
Locks coordinate independent gateway processes; unfinished entries are not answers.
"""
import asyncio
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
import uuid


class CacheError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def validate_config(config, exposed_paths=()):
    if not isinstance(config, dict) or set(config) != {'version', 'directory', 'namespace'} or type(config['version']) is not int or config['version'] != 1:
        raise ValueError('response_cache requires version: 1, directory and namespace')
    if not isinstance(config['namespace'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', config['namespace']):
        raise ValueError('Invalid response cache namespace')
    if not isinstance(config['directory'],str) or not config['directory'].strip():
        raise ValueError('Response cache directory must be a nonempty path')
    directory = Path(config['directory']).resolve()
    for value in exposed_paths:
        if not value:
            continue
        exposed = Path(value).resolve()
        if directory.is_relative_to(exposed) or exposed.is_relative_to(directory):
            raise ValueError('Response cache must be separate from mounted resources and workspaces')
    return dict(config, directory=str(directory))


def sample_path(entry, sample_id=None):
    base = list(entry.get('cache_sample_path', []))
    if sample_id is not None:
        if not isinstance(sample_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', sample_id):
            raise ValueError('Invalid sample ID')
        base.append(sample_id)
    return base


def lease_for(config, entry, identity):
    cache = config.get('response_cache')
    if not cache or entry['wallet'] not in ('development', 'evaluation'):
        return None
    # Development and held-out acceptance never share a response partition.
    key = hashlib.sha256(canonical({'version': 1, 'namespace': cache['namespace'],
        'wallet': entry['wallet'], **identity})).hexdigest()
    return Lease(Path(cache['directory']), key)


class Lease:
    def __init__(self, root, key):
        self.root, self.key, self.lock = root, key, None
        self.entry = root / 'entries' / key[:2] / key

    async def acquire(self, deadline):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        locks = self.root / 'locks'; locks.mkdir(exist_ok=True, mode=0o700)
        self.lock = (locks / self.key).open('a+b')
        while True:
            if time.time() >= deadline:
                raise CacheError('Cache wait reached execution deadline; no provider request issued')
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                await asyncio.sleep(.05)

    def read(self):
        if not self.entry.exists():
            return None
        try:
            raw = (self.entry / 'response.body').read_bytes()
            encoded = (self.entry / 'record.json').read_bytes()
            if hashlib.sha256(encoded).hexdigest() != (self.entry / 'record.sha256').read_text():
                raise ValueError('Record checksum mismatch')
            record = json.loads(encoded)
            if record['key'] != self.key or record['version'] != 1:
                raise ValueError('Identity mismatch')
            if hashlib.sha256(raw).hexdigest() != record['response_sha256']:
                raise ValueError('Response checksum mismatch')
            charge = record['equivalent_charge_usd']
            if type(charge) not in (int, float) or not math.isfinite(charge) or charge < 0:
                raise ValueError('Invalid settled charge')
            if type(record['streaming']) is not bool or not 200 <= record['http_status'] < 300:
                raise ValueError('Invalid completed response')
            if not re.fullmatch('[0-9a-f]{32}', record['source_call_id']):
                raise ValueError('Invalid source call')
            return raw, record
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise CacheError('Invalid response cache entry; reconcile before another call') from error

    def publish(self, raw, *, charge, usage, status, streaming, call_id, ledger_path):
        if self.entry.exists():
            raise CacheError('Cache entries are immutable')
        self.entry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.entry.parent / ('.pending-' + uuid.uuid4().hex)
        temporary.mkdir(mode=0o700)
        record = {'version': 1, 'key': self.key, 'response_sha256': hashlib.sha256(raw).hexdigest(),
            'equivalent_charge_usd': charge, 'usage': usage, 'http_status': status,
            'streaming': streaming, 'source_call_id': call_id, 'source_ledger': str(ledger_path),
            'created': time.time()}
        try:
            encoded=canonical(record)
            for name, data in [('response.body', raw), ('record.json', encoded),
                               ('record.sha256',hashlib.sha256(encoded).hexdigest().encode())]:
                with (temporary / name).open('wb') as out:
                    out.write(data); out.flush(); os.fsync(out.fileno())
            temporary.rename(self.entry)
        finally:
            if temporary.exists(): shutil.rmtree(temporary)

    def close(self):
        if self.lock is not None:
            self.lock.close(); self.lock = None
