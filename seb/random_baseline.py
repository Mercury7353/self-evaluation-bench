"""Frozen random baseline utilities; datasets and credentials live outside Git."""
import ast
import json
import re
import subprocess
import tempfile
from pathlib import Path


def code_text(text):
    blocks=re.findall(r'```(?:python3?|py)?\s*\n(.*?)```',text,re.S)
    return blocks[-1] if blocks else text.strip()


def code_grade(item,text,rootfs,work):
    code=code_text(text)
    if not code:return {'score':0.0,'reason':'empty'}
    with tempfile.TemporaryDirectory(dir=work) as d:
        p=Path(d)/'check.py'
        p.write_text('import resource\nresource.setrlimit(resource.RLIMIT_CPU,(5,5))\nresource.setrlimit(resource.RLIMIT_AS,(1073741824,1073741824))\n'+item.get('prefix','')+'\n'+code+'\n'+item['test']+'\n')
        cmd=['bwrap','--ro-bind',str(rootfs),'/','--unshare-all','--die-with-parent','--new-session','--proc','/proc','--dev','/dev','--tmpfs','/tmp','--ro-bind',str(p),'/tmp/check.py','/usr/local/bin/python','/tmp/check.py']
        try:
            q=subprocess.run(cmd,capture_output=True,timeout=8)
            # Namespace/runtime failures are infrastructure errors, never wrong answers.
            err=q.stderr.decode(errors='replace')
            if 'bwrap:' in err:raise RuntimeError(err)
            return {'score':float(q.returncode==0),'returncode':q.returncode,'stderr':err[-3000:]}
        except subprocess.TimeoutExpired:return {'score':0.0,'reason':'code_timeout'}


def bfcl_grade(item,text):
    """Single-call JSON adaptation; no official BFCL leaderboard equivalence claimed."""
    try:
        answer=json.loads(re.sub(r'^```(?:json)?\s*|\s*```$','',text.strip()))
    except ValueError:return 0.0
    if not isinstance(answer,dict) or set(answer)!={'name','arguments'}:return 0.0
    args=answer['arguments'];gt=item['answer']
    if answer['name']!=item['function_name'] or not isinstance(args,dict):return 0.0
    if set(args)-set(gt):return 0.0
    for k,valid in gt.items():
        if k not in args:
            if k in item['required'] or '' not in valid:return 0.0
        elif not any(type(args[k]) is type(v) and args[k]==v or isinstance(args[k],(int,float)) and not isinstance(args[k],bool) and isinstance(v,(int,float)) and not isinstance(v,bool) and args[k]==v for v in valid):return 0.0
    return 1.0


def grade(item,text,rootfs,work):
    text=text or ''
    if not text.strip():return {'score':0.0,'reason':'empty'}
    kind=item['kind']
    if kind=='code':return code_grade(item,text,rootfs,work)
    if kind=='mcq':
        matches=re.findall(r'(?:^|\n)\s*(?:Answer\s*:\s*)?([ABCD])\s*[.)]?\s*$',text.strip(),re.I)
        return {'score':float(bool(matches) and matches[-1].upper()==item['answer'])}
    if kind=='actions':
        found=re.findall(r'\([^()]+\)',text.lower())
        canonical=lambda s:' '.join(s.strip().lower().split())
        return {'score':float(bool(found) and {canonical(x) for x in found}=={canonical(x) for x in item['answer']})}
    if kind=='bfcl':return {'score':bfcl_grade(item,text)}
    if kind=='ifeval':
        from instruction_following_eval import instructions_registry
        values=[]
        for iid,kwargs in zip(item['instruction_id_list'],item['kwargs']):
            obj=instructions_registry.INSTRUCTION_DICT[iid](iid)
            obj.build_description(**{k:v for k,v in kwargs.items() if v is not None})
            if 'prompt' in (obj.get_instruction_args() or []):obj.build_description(prompt=item['prompt'])
            values.append(bool(obj.check_following(text)))
        return {'score':float(all(values)),'instruction_results':values}
    return broad_grade(item,text,rootfs,work)


def plan_state(text):
    """Blocksworld state normalization, following PlanBench predicate mappings."""
    colors=['red','blue','orange','yellow','white','magenta','black','cyan','green','violet','silver','gold']
    mapping=[('ontable','on the table',1),('clear','clear',1),('handempty','hand is empty',0),('holding','holding',1),('on','on top of',2)]
    out=[]
    for pred in text.lower().replace(' and ', ',').split(','):
        if ' not ' in pred:continue
        for name,phrase,count in mapping:
            if phrase not in pred:continue
            objs=[]
            for segment in pred.split(phrase):
                for i,c in enumerate(colors):
                    if c in segment:objs.append(chr(97+i));break
            out.append('_'.join([name]+objs[:count]));break
    return sorted(out)


def validity(text):
    t=text.lower()
    if re.search(r'\bthe (above )?plan is (not valid|invalid)\b',t):return False
    if re.search(r'\bthe (above )?plan is valid\b',t):return True
    if re.search(r'\binvalid\b',t):return False
    if re.search(r'\bvalid\b',t):return True
    return None


def sandbox_vendor(item,text,rootfs,work):
    """Third-party graders (including code execution/eval) stay inside bwrap."""
    work=Path(work);config=json.loads((work/'run-config.json').read_text())
    with tempfile.TemporaryDirectory(dir=work) as d:
        d=Path(d);(d/'entry.json').write_text(json.dumps({'item':item,'text':text}))
        if item['kind']=='lcb':
            (d/'tests.json').write_bytes(Path(item['input_output_file']).read_bytes())
            script="""import json,sys,resource
resource.setrlimit(resource.RLIMIT_CPU,(90,90))
from testing_util import run_test
x=json.load(open('/tmp/job/entry.json')); tests=json.load(open('/tmp/job/tests.json'))
result,meta=run_test({'input_output':json.dumps(tests)},test=x['text'],timeout=6)
print('SEB_GRADE_JSON:'+json.dumps({'score':float(bool(result) and all(v==True for v in result)),'case_results':[int(v) for v in result],'case_count':len(tests['inputs']),'metadata':meta},default=str))
"""
        else:
            script="""import json,resource
resource.setrlimit(resource.RLIMIT_CPU,(10,10))
import EvaluateFunc
x=json.load(open('/tmp/job/entry.json'));item=x['item'];entry=item['entry'];entry['output']=x['text']
names={'onedoc-repeat':'judge_onedoc_repeat','onedoc-qa':'judge_onedoc_qa','onedoc-extract':'judge_onedoc_extract','list-single_query_id':'judge_label_equal_output','list-multi_query_id':'judge_labels_equal_outputs','list-offset_query_id':'judge_label_equal_output','list-offset_query_element':'judge_label_equal_output','list-blur_offset_query_id':'judge_list_input_blur_offset_query','list-blur_offset_query_element':'judge_list_input_blur_offset_query','multidoc-batch_label':'judge_multidoc_batch_label','multidoc-find_dup_text':'judge_multidoc_find_dup_text'}
scores=getattr(EvaluateFunc,names[item['task']])(entry)
print('SEB_GRADE_JSON:'+json.dumps({'score':scores['total_score'],'details':scores}))
"""
        (d/'run.py').write_text(script)
        cmd=['bwrap','--ro-bind',str(rootfs),'/','--unshare-all','--die-with-parent','--new-session','--proc','/proc','--dev','/dev','--tmpfs','/tmp','--ro-bind',str(d),'/tmp/job','--ro-bind',config['grader_vendor'],'/tmp/graders','--ro-bind',config['numpy_vendor'],'/tmp/numpy','--setenv','PYTHONPATH','/tmp/graders:/tmp/numpy','--setenv','OPENBLAS_NUM_THREADS','1','/usr/local/bin/python','/tmp/job/run.py']
        try:q=subprocess.run(cmd,capture_output=True,timeout=120 if item['kind']=='lcb' else 20)
        except subprocess.TimeoutExpired:
            if item['kind']=='lcb':return {'score':0.0,'reason':'code_total_timeout'}
            raise RuntimeError('LIF grader timeout')
        stdout=q.stdout.decode(errors='replace');stderr=q.stderr.decode(errors='replace')
        matches=re.findall(r'^SEB_GRADE_JSON:(.*)$',stdout,re.M)
        if not matches:
            if item['kind']=='lcb' and 'bwrap:' not in stderr and 'ModuleNotFoundError' not in stderr:
                return {'score':0.0,'reason':'candidate_terminated_runner','returncode':q.returncode,'stderr':stderr[-2000:]}
            raise RuntimeError('vendor grader failed: '+stderr[-2000:])
        result=json.loads(matches[-1]);score=result['score']
        if not isinstance(score,(int,float)) or not 0<=score<=1:raise ValueError('Invalid vendor score')
        return result


def broad_grade(item,text,rootfs,work):
    kind=item['kind']
    if kind=='integer':
        matches=re.findall(r'(?:Answer\s*:\s*|\\boxed\{)([0-9]+)',text,re.I)
        if not matches and re.fullmatch(r'\s*[0-9]+\s*',text):matches=[text.strip()]
        return {'score':float(bool(matches) and int(matches[-1])==item['answer'])}
    if kind=='next_line':
        lines=[x.strip() for x in code_text(text).splitlines() if x.strip() and not x.strip().startswith('#')]
        return {'score':float(bool(lines) and lines[0]==item['answer'].strip())}
    if kind=='plan_validity':return {'score':float(validity(text) is not None and validity(text)==validity(item['answer']))}
    if kind=='plan_state':
        if '[RESULTING STATE]' in text:text=text.rsplit('[RESULTING STATE]',1)[-1]
        actual=plan_state(text);expected=item['answer'] if isinstance(item['answer'],list) else plan_state(item['answer'])
        return {'score':float(actual==sorted(expected)),'extracted':actual}
    if kind=='ndcg':
        import math
        try:rank=json.loads(re.sub(r'^```(?:json)?\s*|\s*```$','',text.strip()))
        except ValueError:return {'score':0.0,'reason':'invalid_ranking_json'}
        if not isinstance(rank,list) or any(not isinstance(x,str) for x in rank) or len(rank)!=len(item['doc_ids']) or set(rank)!=set(item['doc_ids']):return {'score':0.0,'reason':'ranking_must_be_permutation'}
        rels=item['qrels'];dcg=lambda xs:sum(v/math.log2(i+2) for i,v in enumerate(xs[:10]))
        ideal=dcg(sorted(rels.values(),reverse=True))
        return {'score':dcg([rels.get(x,0) for x in rank])/ideal if ideal else 0.0}
    if kind in ('lifbench','lcb'):return sandbox_vendor(item,code_text(text) if kind=='lcb' else text,rootfs,work)
    raise ValueError(kind)
