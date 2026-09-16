"""Controller-owned submission snapshots and evidence-based research stopping."""
import ast
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time

from .evaluation import load_manifest
from .research import validate_submission, write_json
from .runner import digest_tree


class ResearchLifecycle:
    """Save structurally valid work without inspecting scores or hidden labels.

    The archive is outside all researcher mounts. Polling saves observed versions,
    not every intermediate edit. Executability and scientific quality are still
    independently tested during acceptance.
    """
    def __init__(self, config, work, output, researcher, *, minimum_items, require_predictor,
                 resume=False, started=None):
        self.config = config
        self.source = Path(work) / 'submission'
        self.root = Path(output) / 'research-checkpoints'
        self.root.mkdir(exist_ok=resume)
        self.researcher = researcher
        self.minimum_items = minimum_items
        self.require_predictor = require_predictor
        self.started = time.time() if started is None else started
        self.deadline = config['research_deadline_epoch']
        self.entries = []
        if resume and (self.root/'index.json').exists():
            self.entries = json.loads((self.root/'index.json').read_text())
            for index, entry in enumerate(self.entries, 1):
                if (entry['sequence'] != index or Path(entry['path']) != self.root/f'{index:06d}'
                        or digest_tree(entry['path']) != entry['files']):
                    raise ValueError('Saved research checkpoint changed before continuation')
        self.last_check = 0
        self.reason = None
        self.evidence = None
        self.last_invalid = None
        if resume and (self.root/'last-invalid.json').exists():
            self.last_invalid = json.loads((self.root/'last-invalid.json').read_text())
        self.inspected_requests = set()

    def capture(self, *, force=False):
        now = time.time()
        if now >= self.deadline or not force and now - self.last_check < 5:
            return
        self.last_check = now
        temporary = self.root / 'staging'
        try:
            hashes = validate_submission(self.source)
            load_manifest(self.source, self.minimum_items, domains=self.config.get('joint_domains'), minimum_questions=self.config.get('minimum_questions',0))
            required = ['run.py'] + (['predictor.py'] if self.require_predictor else [])
            for name in required:
                ast.parse((self.source / name).read_text(), filename=name)
            fingerprint = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
            if self.entries and self.entries[-1]['submission_id'] == fingerprint:
                return
            shutil.copytree(self.source, temporary)
            if digest_tree(temporary) != hashes or digest_tree(self.source) != hashes:
                raise ValueError('Submission changed during checkpoint capture')
            # Copying must finish within the original research window too.
            if time.time() >= self.deadline:
                return
            name = f'{len(self.entries) + 1:06d}'
            temporary.rename(self.root / name)
            entry = {'sequence': len(self.entries) + 1, 'path': str(self.root / name),
                     'submission_id': fingerprint, 'files': hashes, 'captured_at': time.time(),
                     'validation': 'static interface and entry-point syntax; execution not yet verified'}
            self.entries.append(entry)
            write_json(self.root / 'index.json', self.entries)
        except (ValueError, SyntaxError, OSError, TypeError, KeyError) as error:
            self.last_invalid = {'at': now, 'error': type(error).__name__ + ': ' + str(error)}
            write_json(self.root / 'last-invalid.json', self.last_invalid)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def poll(self, *, allow_time_stop=True):
        """Called by the host process supervisor; never trusts model-written text."""
        if self.reason:
            return self.reason
        self.capture()
        artifacts = Path(self.config['artifacts'])
        blocked = artifacts / 'native-upstream-blocked.json'
        if blocked.exists():
            self.reason = 'upstream_blocked'
            self.evidence = {'path': str(blocked), **json.loads(blocked.read_text())}
        else:
            # A closed accounting wallet must not be relabeled as ordinary D exhaustion.
            ledger = artifacts / 'ledger.sqlite'
            if ledger.exists():
                with sqlite3.connect(f'file:{ledger}?mode=ro', uri=True) as db:
                    wallet = db.execute("SELECT name,cap FROM wallets WHERE name IN ('designer','development') AND cap=0 ORDER BY name").fetchone()
                if wallet:
                    self.reason = 'researcher_accounting_guard' if wallet[0]=='designer' else 'development_accounting_guard'
                    self.evidence = {'ledger': str(ledger), 'wallet': wallet[0], 'cap': 0}
            rejected = []
            if not self.reason:
                for kind in ('wire', 'native-wire'):
                    for path in (artifacts / kind).glob('*/meta.json'):
                        if path in self.inspected_requests:
                            continue
                        try:
                            meta = json.loads(path.read_text())
                        except (OSError, ValueError):
                            continue
                        self.inspected_requests.add(path)
                        if (meta.get('wallet') == 'designer' and meta.get('model') == self.researcher
                                and meta.get('created', 0) >= self.started
                                and meta.get('state') == 'rejected_budget'):
                            rejected.append({'path': str(path), **meta})
                if rejected:
                    self.reason = 'researcher_budget_limit'
                    self.evidence = min(rejected, key=lambda r: (r['created'], r['id']))
            if not self.reason:
                for path in sorted((artifacts / 'limit-events').glob('*.json')):
                    event = json.loads(path.read_text())
                    if (event.get('wallet') == 'designer' and event.get('models') == [self.researcher]
                            and event.get('at', 0) >= self.started
                            and event.get('reason') == 'research_time_limit'
                            and event.get('deadline_epoch') == self.deadline):
                        self.reason = 'research_time_limit'
                        self.evidence = {'path': str(path), **event}
                        break
            if not self.reason and allow_time_stop and time.time() >= self.deadline:
                self.reason = 'research_time_limit'
                self.evidence = {'deadline_epoch': self.deadline, 'observed_at': time.time(),
                                 'source': 'host process monitor'}
        if self.reason:
            write_json(self.root / 'stop.json', {'reason': self.reason, 'detected_at': time.time(),
                                               'evidence': self.evidence})
        return self.reason

    def finish(self, trace, returncode, *, harness):
        self.poll(allow_time_stop=False)
        process = Path(trace) / ('codex.process.json' if harness == 'codex' else 'claude.process.json')
        info = json.loads(process.read_text()) if process.exists() else {}
        # Only the host's actual timeout, not a model claiming it ran out of time.
        timed_out = returncode == 124 and info.get('termination_reason') == 'timeout'
        if not self.reason and timed_out:
            self.reason = ('checkpoint_time_limit' if info['started'] + info['timeout_seconds'] < self.deadline - 1
                           else 'research_time_limit')
            self.evidence = {'process_file': str(process), 'deadline_epoch': self.deadline}
        self.capture(force=True)
        outcome = {'reason': self.reason or 'completed', 'returncode': returncode,
                   'expected_resource_stop': self.reason in ('researcher_budget_limit', 'research_time_limit', 'checkpoint_time_limit'),
                   'evidence': self.evidence, 'finished_at': time.time(),
                   'saved_valid_snapshots': len(self.entries), 'latest_invalid': self.last_invalid}
        write_json(self.root / 'outcome.json', outcome)
        return outcome

    def select(self):
        if not self.entries:
            raise RuntimeError('Researcher produced no valid saved submission before the deadline')
        selected = self.entries[-1]
        if digest_tree(selected['path']) != selected['files']:
            raise ValueError('Saved research checkpoint changed after capture')
        return selected
