import json
import pytest
import yaml
from seb.experiment_config import load, researcher_view
from seb.experiment import build_gateway, prepare_workspace
from test_experiment import joint_fixture


def split_config(tmp_path):
    path,cfg=joint_fixture(tmp_path)
    cfg['domain_protocol']['version']=3
    cfg['domain_protocol']['score_mode']='raw_domain'
    cfg['overall']={'metric':'macro_pearson','source':'raw_domain'}
    cfg['models'].append({'id':'new-sealed','family':'new-family','split':'holdout','availability':'pending','pending_reason':'endpoint unavailable'})
    cfg['visibility_contract']={
        'version':1,
        'development_models':[m['id'] for m in cfg['models'] if m['split']=='development'],
        'sealed_models':[m['id'] for m in cfg['models'] if m['split']=='holdout'],
        'visible_targets':[t['id'] for t in cfg['benchmarks']['whitebox']],
        'sealed_targets':[t['id'] for t in cfg['benchmarks']['blackbox']],
    }
    path.write_text(yaml.safe_dump(cfg));return path,cfg


def test_two_axes_do_not_expose_new_models_or_sealed_targets(tmp_path):
    path,cfg=split_config(tmp_path);loaded=load(path)
    out=tmp_path/'run';out.mkdir()
    config,tokens=build_gateway(loaded,loaded['researchers'][0],out,tmp_path/'sock',mock_url='http://localhost:9')
    work=prepare_workspace(loaded,config,tokens,out)
    view=json.loads((work/'whitebox.json').read_text())
    assert set(view['models'])==set(cfg['visibility_contract']['development_models'])
    assert set(view['targets'])==set(cfg['visibility_contract']['visible_targets'])
    assert config['tokens'][tokens['development']]['candidate_models']==view['models']
    assert set(config['tokens'][tokens['evaluation']]['models'])=={m['id'] for m in cfg['models'] if m.get('availability')!='pending'}
    material='\n'.join(p.read_text() for p in work.rglob('*') if p.is_file())
    for hidden in cfg['visibility_contract']['sealed_models']+cfg['visibility_contract']['sealed_targets']:
        assert hidden not in material
    assert 'visibility_contract' not in json.dumps(researcher_view(loaded))


@pytest.mark.parametrize('mutation',['model_to_dev','target_to_visible','missing_new_model','unbalanced_domains','unbalanced_targets'])
def test_partition_drift_fails_before_gateway(tmp_path,mutation):
    path,cfg=split_config(tmp_path)
    if mutation=='model_to_dev':cfg['models'][3]['split']='development'
    elif mutation=='target_to_visible':cfg['benchmarks']['whitebox'].append(cfg['benchmarks']['blackbox'].pop())
    elif mutation=='missing_new_model':cfg['models'].pop()
    elif mutation=='unbalanced_domains':
        cfg['benchmarks']['whitebox'][0]['domain']='reasoning'
        cfg['benchmarks']['whitebox'][1]['domain']='reasoning'
    else:
        row=cfg['benchmarks']['blackbox'].pop();cfg['visibility_contract']['sealed_targets'].remove(row['id'])
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError):load(path)
