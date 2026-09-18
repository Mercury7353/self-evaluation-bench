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
    raise ValueError(kind)
