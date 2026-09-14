"""Score newly admitted reference labels against saved, unchanged predictions.

This command has no provider or researcher execution path. Source compatibility
must be established by the operator before submitting labels; hashes alone do
not establish protocol equivalence. Original results and ledgers remain intact.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re

from .domain_scoring import finite, reference_status, summarize_joint, summarize_outputs


def rescore(scoring_input, additions, output):
    scoring_input=Path(scoring_input).resolve();additions=Path(additions).resolve()
    output=Path(output).resolve()
    if output.exists():raise FileExistsError('Use a new output directory; preserve earlier reports')
    raw=scoring_input.read_bytes();extra_raw=additions.read_bytes()
    frozen=json.loads(raw);extra=json.loads(extra_raw)
    if frozen.get('version')!=1 or extra.get('version')!=1:
        raise ValueError('Reference scoring input and additions require version: 1')
    if set(extra)!={'version','scores','evidence'}:
        raise ValueError('Additions contain scores/evidence only; predictions and panels cannot change')
    if not isinstance(extra['scores'],dict) or not extra['scores']:
        raise ValueError('Provide nonempty target-to-model score additions')
    if not isinstance(extra['evidence'],dict) or set(extra['scores'])!=set(extra['evidence']):
        raise ValueError('Every added target requires source evidence')
    references=copy.deepcopy(frozen['references']);panels=frozen['reference_panels'];added=[]
    for target,scores in extra['scores'].items():
        if target not in panels:raise ValueError('Cannot add or rename a target')
        if not isinstance(scores,dict) or not scores or not isinstance(extra['evidence'][target],dict) or set(scores)!=set(extra['evidence'][target]):
            raise ValueError('Each nonempty model score needs matching source evidence')
        refs=references.setdefault(target,{})
        for model,score in scores.items():
            if model not in panels[target]:raise ValueError('Cannot change the frozen held-out panel')
            if model in refs:raise ValueError('Cannot replace an existing reference score')
            if not finite(score):raise ValueError('Reference scores must be finite')
            ev=extra['evidence'][target][model]
            if not isinstance(ev,dict) or not isinstance(ev.get('source'),str) or not ev['source'].strip() or not re.fullmatch('[0-9a-f]{64}',str(ev.get('sha256',''))):
                raise ValueError('Reference evidence requires a source and SHA-256')
            refs[model]=score;added.append({'target':target,'model':model,'score':score,'evidence':ev})
    limits={'minimum_models':frozen['minimum_models'],'minimum_families':frozen['minimum_families'],
            'reference_panels':panels}
    if frozen['domains'] is not None:
        report=summarize_joint(frozen['models'],references,frozen['domains'],
            frozen['predictions'],frozen['domain_scores'],**limits)
    else:
        report=summarize_outputs(frozen['models'],references,frozen['visible_targets'],frozen['sealed_targets'],
            frozen['predictions'],frozen['domain_scores'],**limits)
    report.update(reference_status(report),predictions=frozen['predictions'],domain_scores=frozen['domain_scores'],
        execution_kind='reference_addition_only',new_model_calls=0,new_predictor_fits=0,
        source_scoring_input=str(scoring_input),source_scoring_input_sha256=hashlib.sha256(raw).hexdigest(),
        additions_sha256=hashlib.sha256(extra_raw).hexdigest(),added_references=added,
        note='Operator-admitted source labels; this calculation does not verify source protocol compatibility or replace original execution/accounting status.')
    output.mkdir(parents=True,exist_ok=False)
    (output/'scores.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    updated=dict(frozen,references=references)
    (output/'reference-scoring-input.json').write_text(json.dumps(updated,indent=2,allow_nan=False)+'\n')
    (output/'reference-additions.json').write_bytes(extra_raw)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scoring_input',type=Path)
    parser.add_argument('--additions',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=rescore(args.scoring_input,args.additions,args.output)
    print(json.dumps({k:result[k] for k in ('reference_status','pending_reference_targets','new_model_calls','new_predictor_fits')}))


if __name__=='__main__':main()
