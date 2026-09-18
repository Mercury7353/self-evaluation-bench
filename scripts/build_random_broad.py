from pathlib import Path
import json,random,hashlib,pandas as pd,re,gzip,collections,importlib.util,pickle,io,zlib,base64
import argparse
parser=argparse.ArgumentParser();parser.add_argument('artifact_root');args=parser.parse_args()
R=Path(args.artifact_root);D=R/'data';P=R/'pace-source';B=R/'broad';B.mkdir(exist_ok=True)
pools={}
def take(name,rows,n):
 rows=sorted(rows,key=lambda x:x['id']);assert len(rows)>=n,(name,len(rows));pools[name]=rows
rows=[]
for d in [json.loads(x) for x in (D/'mbpp.raw').read_text().splitlines()]:
 if 11<=d['task_id']<=510:
  rows.append({'id':'mbpp:'+str(d['task_id']),'source':'mbpp','domain':'coding','kind':'code','prompt':'Write a complete Python solution. Return only Python code.\n'+d['text']+'\nYour code must satisfy:\n'+'\n'.join(d['test_list']),'prefix':'','test':d.get('test_setup_code','')+'\n'+'\n'.join(d['test_list']),'oracle':d['code']})
take('mbpp',rows,80)
rows=[]
for d in [json.loads(x) for x in gzip.decompress((D/'humaneval.gz').read_bytes()).decode().splitlines()]:
 rows.append({'id':d['task_id'],'source':'humaneval','domain':'coding','kind':'code','prompt':'Complete the following Python function. Return the complete Python code including the function signature.\n'+d['prompt'],'prefix':'from typing import *\n','test':d['test']+'\ncheck('+d['entry_point']+')','oracle':d['prompt']+d['canonical_solution']})
take('humaneval',rows,60)
lcd={x['task_id']:x for x in pd.read_parquet(D/'lcd-train.parquet').to_dict('records')};rows=[]
debug_files={}
for a in json.loads((R/'debug-audit.json').read_text()):
 if not a['eligible']:continue
 if a['source_file'] not in debug_files:debug_files[a['source_file']]=json.loads((R/a['source_file']).read_text())
 d=debug_files[a['source_file']][a['source_index']];x=lcd[a['slug']]
 rows.append({'id':'debugbench:'+a['id'],'source':'debugbench','domain':'coding','kind':'code','slug':a['slug'],'prompt':'Repair the buggy Python code for this problem. Return only the complete corrected Python code.\n'+d['description']+'\nConstraints:\n'+str(d['constraints'])+'\nBuggy code:\n'+d['buggy_code'],'prefix':x['prompt'],'test':x['test']+'\ncheck('+x['entry_point']+')','oracle':d['oracle_code'],'test_provenance':'newfacade/LeetCodeDataset train; generated third-party tests, not LeetCode official hidden tests','test_count':a['test_count']})
# One bug per problem prevents near-duplicate slugs dominating the random pool.
unique={}
for d in sorted(rows,key=lambda x:x['id']):unique.setdefault(d['slug'],d)
take('debugbench',list(unique.values()),60)
rows=[]
for d in [json.loads(x) for x in (D/'ifeval.raw').read_text().splitlines()]:
 rows.append({**d,'id':'ifeval:'+str(d['key']),'source':'ifeval','domain':'co-work','kind':'ifeval'})
take('ifeval',rows,80)
answers={d['id']:d for d in [json.loads(x) for x in (D/'bfcl-answers.jsonl').read_text().splitlines()]};rows=[]
for d in [json.loads(x) for x in (D/'bfcl.jsonl').read_text().splitlines()]:
 f=d['function'];gt=answers[d['id']]['ground_truth']
 if len(f)!=1 or len(gt)!=1 or f[0]['name'] not in gt[0]:continue
 props=f[0]['parameters']['properties']
 # Flat scalar API arguments only; complex AST semantics are out of this adaptation.
 if any(x.get('type') not in ['string','integer','float','number','boolean'] for x in props.values()):continue
 if len(d['question'])!=1 or len(d['question'][0])!=1:continue
 ans=gt[0][f[0]['name']]
 if any(not isinstance(v,list) or any(isinstance(z,(list,dict)) for z in v) for v in ans.values()):continue
 rows.append({'id':'bfcl:'+d['id'],'source':'bfcl','domain':'co-work','kind':'bfcl','prompt':'Choose the arguments for this function call. Return only a JSON object with exactly keys "name" and "arguments".\nFunction schema:\n'+json.dumps(f[0])+'\nUser request:\n'+d['question'][0][0]['content'],'function_name':f[0]['name'],'required':f[0]['parameters'].get('required',[]),'answer':ans})
take('bfcl',rows,60)
rows=[]
for d in pd.read_parquet(D/'acp_bench0.parquet').to_dict('records'):
 rows.append({'id':'acp:'+str(d['id']),'source':'acp','domain':'co-work','kind':'actions','prompt':d['context']+'\n'+d['question']+'\nReturn only the complete list of parenthesized actions.','answer':list(d['answer'])})
take('acp',rows,60)
rows=[]
for i,block in enumerate((D/'logiqa.raw').read_text().strip().split('\n\n')):
 lines=block.splitlines()
 if len(lines)<7 or lines[0].strip() not in ['a','b','c','d']:continue
 rows.append({'id':'logiqa:'+str(i),'source':'logiqa','domain':'reasoning','kind':'mcq','prompt':'\n'.join(lines[1:])+'\nAnswer with only A, B, C, or D.','answer':lines[0].upper()})
take('logiqa',rows,100)
rows=[]
for i,d in enumerate(pd.read_parquet(D/'mmlu0.parquet').to_dict('records')):
 rows.append({'id':'mmlu:'+str(i),'source':'mmlu','domain':'reasoning','kind':'mcq','subject':d['subject'],'prompt':d['question']+'\n'+'\n'.join(chr(65+j)+'. '+x for j,x in enumerate(d['choices']))+'\nAnswer with only A, B, C, or D.','answer':chr(65+d['answer'])})
take('mmlu',rows,100)
def add(name,rows):
 pools[name]=sorted(rows,key=lambda x:x['id']);print(name,len(rows),flush=True)
# Public raw docs only, never stored previous model outputs or scores.
p=next((P/'results/raw_results/gpqa/azure__o3').glob('samples_gpqa_diamond*'));unique={}
for line in p.open():
 d=json.loads(line)['doc'];unique[d['Record ID']]=d
rows=[]
for key,d in sorted(unique.items()):
 opts=[d['Correct Answer']]+[d[f'Incorrect Answer {i}'] for i in range(1,4)];order=list(range(4));random.Random('42:gpqa:'+key).shuffle(order)
 rows.append(dict(id='gpqa:'+key,source='gpqa',domain='reasoning',kind='mcq',prompt=d['Question']+'\n'+'\n'.join(chr(65+i)+'. '+opts[j] for i,j in enumerate(order))+'\nAnswer with only A, B, C, or D.',answer=chr(65+order.index(0))))
add('gpqa',rows)
p=next((P/'results/raw_results/aime25/azure__gpt-5.2').glob('samples*'));unique={}
for line in p.open():
 d=json.loads(line)['doc'];unique[d['id']]=d
add('aime25',[dict(id='aime25:'+key,source='aime25',domain='reasoning',kind='integer',prompt=d['problem']+'\nEnd your answer with: Answer: <integer>.',answer=int(d['answer'])) for key,d in unique.items()])
# RepoBench v1.1 Python cross-file-first, December / 8k, matching PACE default subset.
spec=importlib.util.spec_from_file_location('repo_prompt',P/'evaluations/benchmarks/repobench/data/utils.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
rows=[]
for p in sorted(D.glob('repobench-first-*.parquet')):
 for i,d in enumerate(pd.read_parquet(p, columns=['repo_name','file_path','context','import_statement','cropped_code','next_line','created_at','level'],filters=[('level','=','8k')],use_threads=False).to_dict('records')):
  if d['level']!='8k' or not '2023-12-01'<=d['created_at'][:10]<='2023-12-31':continue
  prompt='Continue the following code. Output only the next line of code, no explanation or markdown.\n'+mod.construct_prompt(d)
  rows.append(dict(id='repobench:'+p.stem+':'+str(i),source='repobench',domain='coding',kind='next_line',prompt=prompt,answer=d['next_line'],level=d['level']))
add('repobench',rows)
# InFoBench frozen decomposed criteria, one fixed external judge across candidates.
p=next((P/'evaluations/benchmarks/infobench/output_files').glob('*.jsonl'));rows=[]
for line in p.open():
 d=json.loads(line);prompt=('Input:\n'+d['input']+'\n\n' if d.get('input') else '')+'Instruction:\n'+d['instruction']
 rows.append(dict(id='infobench:'+d['id'],source='infobench',domain='co-work',kind='infobench',prompt=prompt,criteria=d['decomposed_questions']))
add('infobench',rows)
# LIFBench complete prompts up to length13 (~13k native tokens), no truncation.
rows=[]
for p in sorted((P/'evaluations/benchmarks/lifbench/data/prompts').glob('*.json')):
 for d in json.loads(p.read_text()):
  if d['length']>13:continue
  rows.append(dict(id=f"lifbench:{p.stem}:{d['ins_id']}:{d['param_id']}:{d['length']}",source='lifbench',domain='co-work',kind='lifbench',prompt=d['prompt'],task=p.stem,entry=d))
add('lifbench',rows)
# PlanBench: all available state-execution / validity tasks; no model outputs used.
rows=[]
for task in ['task_3_plan_verification','task_7_plan_execution']:
 p=sorted((P/'results/raw_results/planbench').glob('*/*/'+task+'.json'))[0]
 for d in json.loads(p.read_text())['instances']:
  rows.append(dict(id='planbench:'+task+':'+str(d['instance_id']),source='planbench',domain='co-work',kind='plan_validity' if task.startswith('task_3') else 'plan_state',prompt=d['query'],answer=d['ground_truth_plan'],task=task))
add('planbench',rows)
# NFCorpus full test query pool with frozen top20 retrieved docs; never expose qrels.
N=P/'evaluations/benchmarks/beir/datasets/nfcorpus';corpus={d['_id']:d for d in map(json.loads,(N/'corpus.jsonl').open())};queries={d['_id']:d['text'] for d in map(json.loads,(N/'queries.jsonl').open())}
qrels=collections.defaultdict(dict)
for line in (N/'qrels/test.tsv').read_text().splitlines()[1:]:
 q,doc,rel=line.split('\t');qrels[q][doc]=int(rel)
retr=json.loads((P/'evaluations/benchmarks/beir/beir_results/nfcorpus/first_stage_results.json').read_text());rows=[]
for q,rels in sorted(qrels.items()):
 if q not in retr:continue
 docs=sorted(retr[q],key=lambda k:(-retr[q][k],k))[:20]
 prompt='Rank all 20 documents by relevance to the query. Return only a JSON array of all document IDs, most relevant first.\nQuery: '+queries[q]+'\n\n'+'\n\n'.join('ID: '+k+'\nTitle: '+corpus[k]['title']+'\n'+corpus[k]['text'] for k in docs)
 rows.append(dict(id='beir:'+q,source='beir',domain='co-work',kind='ndcg',prompt=prompt,doc_ids=docs,qrels=rels))
add('beir',rows)
# LCB index offsets before draw. Full original public+private tests retained on disk.
class SafeUnpickler(pickle.Unpickler):
 def find_class(self,*a):raise pickle.UnpicklingError('no globals allowed')
def tests(d):
 try:private=json.loads(d['private_test_cases'])
 except ValueError:private=json.loads(SafeUnpickler(io.BytesIO(zlib.decompress(base64.b64decode(d['private_test_cases'])))).load())
 return json.loads(d['public_test_cases'])+private
rows=[];excluded=collections.Counter()
for p in sorted(D.glob('lcb-test*.jsonl')):
 with p.open('rb') as f:
  while True:
   offset=f.tell();line=f.readline()
   if not line:break
   d=json.loads(line)
   try:t=tests(d)
   except Exception:excluded['invalid_test_pack']+=1;continue
   if not t:excluded['empty_tests']+=1;continue
   if len(t)>1000 or len(json.dumps(t))>16*1024*1024:excluded['test_pack_over_1000_cases_or_16MiB']+=1;continue
   if '<img' in d['question_content'].lower() or re.search(r'!\[.*?\]\(',d['question_content']):excluded['image_reference']+=1;continue
   prompt='Solve this programming problem in Python. Return only complete Python code.\n'+d['question_content']
   if d['starter_code']:prompt+='\nUse this interface:\n'+d['starter_code']
   else:prompt+='\nRead input from stdin and write the answer to stdout.'
   rows.append(dict(id='livecodebench:'+d['platform']+':'+d['question_id'],source='livecodebench',domain='coding',kind='lcb',prompt=prompt,test_file=str(p),test_offset=offset,test_count=len(t),contest_date=d['contest_date']))
add('livecodebench',rows)
# Common max input length is a predeclared budget constraint, not text truncation.
eligibility={};full=[]
for name,rows in pools.items():
 unique={};too_long=0
 for x in rows:
  if len(x['prompt'].encode())>64000:too_long+=1;continue
  signature=hashlib.sha256(x['prompt'].encode()).hexdigest()
  unique.setdefault(signature,x)
 pools[name]=sorted(unique.values(),key=lambda x:x['id']);eligibility[name]={'before_common_filter':len(rows),'over_64k_utf8_bytes':too_long,'eligible':len(unique)};full+=pools[name]
quotas={k:8 for k in ['mbpp','humaneval','debugbench','livecodebench','repobench']};quotas.update({k:10 for k in ['logiqa','mmlu','gpqa','aime25']})
cowork=['ifeval','bfcl','acp','infobench','lifbench','planbench','beir'];extra=random.Random('42:co-work-sources').sample(cowork,5);quotas.update({k:5+int(k in extra) for k in cowork})
selected=[]
for name,n in quotas.items():
 rows=pools[name]
 # MMLU stratify by 10 randomly chosen distinct subjects; random question within each.
 if name=='mmlu':
  subjects=sorted({x['subject'] for x in rows});chosen=random.Random('42:mmlu-subjects').sample(subjects,n)
  picked=[random.Random('42:mmlu:'+sub).choice([x for x in rows if x['subject']==sub]) for sub in chosen]
 else:picked=random.Random('42:'+name).sample(rows,n)
 selected+=picked
for x in selected:
 if x['kind']=='lcb':
  with open(x['test_file'],'rb') as f:f.seek(x['test_offset']);d=json.loads(f.readline())
  t=tests(d);pack={'inputs':[a['input'] for a in t],'outputs':[a['output'] for a in t],'fn_name':json.loads(d['metadata']).get('func_name')}
  path=B/(hashlib.sha256(x['id'].encode()).hexdigest()[:20]+'.tests.json');path.write_text(json.dumps(pack));x['input_output_file']=str(path);x['test_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
assert len(selected)==120 and len({x['id'] for x in selected})==120
for name,obj in [('pool.json',full),('suite.json',selected),('eligibility.json',{'sources':eligibility,'lcb_exclusions':dict(excluded),'quota':quotas})]:
 p=B/name;p.write_text(json.dumps(obj,ensure_ascii=False,indent=2));print(name,len(obj),hashlib.sha256(p.read_bytes()).hexdigest(),flush=True)
print(eligibility,quotas,flush=True)
