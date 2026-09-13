from concurrent.futures import ThreadPoolExecutor
import copy
import pytest
from seb.campaign import Registry, episodes


def manifest():
    return {'researchers': [{'id': f'r{i}'} for i in range(16)],
        'candidates': [{'id': f'c{i}', 'split': 'development' if i < 12 else 'holdout'} for i in range(24)],
        'domains': [{'id': f'd{i}', 'targets': [{'id': f't{i}{j}', 'visibility': 'visible' if j < 2 else 'sealed'} for j in range(4)]} for i in range(3)],
        'budgets': {'development_usd': 100, 'researcher_usd': 200, 'candidate_suite_usd': 10, 'research_seconds': 36000},
        'budget_ablation': {'researchers': ['r0', 'r1'], 'additional_development_usd': [20, 300]},
        'baselines': ['random', 'stratified', 'fixed'], 'run_allocation_ceiling_usd': 27900}


def test_matrix_matches_authorized_scope_and_budget():
    rows = episodes(manifest())
    assert len(rows) == 69
    assert sum(r['kind'] == 'main' for r in rows) == 48
    assert sum(r['kind'] == 'budget' for r in rows) == 12
    assert sum(r['development_usd'] for r in rows) == 7620
    assert sum(r['evaluation_usd'] for r in rows) == 8280
    assert sum(r['researcher_usd'] for r in rows) == 12000
    # Middle-budget ablations must reuse main, not get another paid run.
    m = manifest(); m['budget_ablation']['additional_development_usd'].append(100)
    with pytest.raises(ValueError, match='reuses main'): episodes(m)


def test_registry_restart_preserves_claims_and_cannot_raise_budget(tmp_path):
    m = manifest(); reg = Registry(tmp_path, m)
    eid = episodes(m)[0]['id']; reg.claim(eid, 'systemd:test-episode.service')
    again = Registry(tmp_path, m)
    with pytest.raises(ValueError, match='Already claimed'): again.claim(eid, 'new-service')
    changed = copy.deepcopy(m); changed['run_allocation_ceiling_usd'] += 100
    with pytest.raises(ValueError, match='immutable'): Registry(tmp_path, changed)
    assert next(r for r in again.inventory() if r['id'] == eid)['handle'] == 'systemd:test-episode.service'


def test_concurrent_launch_claim_has_exactly_one_winner(tmp_path):
    reg = Registry(tmp_path, manifest()); eid = episodes(manifest())[0]['id']
    def claim(i):
        try: reg.claim(eid, f'unit-{i}'); return True
        except ValueError: return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(claim, range(12))) == 1
