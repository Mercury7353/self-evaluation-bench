"""Standard-library client for independently executed evaluation suites."""
import json
import os
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path


class EvaluationError(RuntimeError):
    def __init__(self,status,payload,headers):
        error=payload.get('error',{})
        self.category=error.get('type','request_error') if isinstance(error,dict) else 'request_error'
        self.details=payload
        self.status=status
        self.operation_id=headers.get('x-seb-operation-id')
        ident=headers.get('x-seb-request-id')
        self.evidence=[{'kind':'llm','id':ident}] if ident and status!=402 else []
        message=error.get('message','') if isinstance(error,dict) else str(error)
        super().__init__(f'HTTP {status}: {self.category}; operation={self.operation_id}; {message[:500]}')


class Client:
    def __init__(self, context=None):
        path=context or os.environ.get('SEB_CONTEXT','/workspace/access.json')
        self.context=json.loads(Path(path).read_text())
        self.base=os.environ.get('SEB_GATEWAY_URL') or self.context.get('base_url','http://127.0.0.1:18765')
        self.token=self.context['token']
        self.artifacts=Path(self.context.get('output_dir','/workspace/client-artifacts'))
        self.artifacts.mkdir(parents=True,exist_ok=True)

    def request(self,path,data=None,timeout=None,operation_id=None,item_id=None,sample_id=None):
        timeout=timeout or self.context.get('client_timeout_seconds',650)
        payload=None if data is None else json.dumps(data).encode()
        headers={'x-api-key':self.token,'Content-Type':'application/json'}
        if operation_id:headers['x-seb-operation-id']=operation_id
        if item_id is not None:headers['x-seb-item-id']=str(item_id)
        if sample_id is not None:headers['x-seb-sample-id']=sample_id
        req=urllib.request.Request(self.base+path,data=payload,headers=headers)
        try:
            with urllib.request.urlopen(req,timeout=timeout) as r:
                return r.read(),dict(r.headers)
        except urllib.error.HTTPError as e:
            message=e.read().decode(errors='replace')
            if self.context.get('protocol_version')==1:
                try:payload=json.loads(message)
                except ValueError:payload={'error':{'type':'invalid_gateway_response'}}
                raise EvaluationError(e.code,payload,dict(e.headers)) from e
            raise RuntimeError(f'HTTP {e.code}: {message}') from e

    def budget(self):
        return json.loads(self.request('/budget')[0])

    def info(self):
        return json.loads(self.request('/research/info')[0])

    def feedback(self,job_ids):
        return json.loads(self.request('/research/feedback',{'job_ids':job_ids},timeout=7200)[0])

    def chat(self,prompt,*,model=None,max_tokens=None,system=None,messages=None,operation_id=None,item_id=None,sample_id=None):
        model=model or self.context.get('model')
        if not model:raise ValueError('Specify a model or use the evaluation context model')
        policy=self.context.get('evaluation_policy')
        if max_tokens is None:max_tokens=policy['default_output_tokens'] if policy else 2048
        if policy and (type(max_tokens) is not int or not policy['min_output_tokens']<=max_tokens<=policy['max_output_tokens']):
            raise ValueError('Output limit violates the frozen policy; no silent clamp')
        body={'model':model,'max_tokens':max_tokens,'messages':messages or [{'role':'user','content':prompt}]}
        if system:body['system']=system
        effort=self.context.get('efforts',{}).get(model)
        if effort:body['output_config']={'effort':effort}
        operation_id=operation_id or uuid.uuid4().hex
        if not all(c.isalnum() or c in '_-' for c in operation_id) or not 1<=len(operation_id)<=128:
            raise ValueError('Invalid operation ID')
        (self.artifacts/(operation_id+'.request.json')).write_text(json.dumps(body))
        raw,headers=self.request('/anthropic/v1/messages',body,operation_id=operation_id,item_id=item_id,sample_id=sample_id)
        data=json.loads(raw);call_id=headers.get('x-seb-request-id')
        if not call_id:raise RuntimeError('Gateway did not supply an evidence ID')
        (self.artifacts/(call_id+'.response.json')).write_bytes(raw)
        return {'text':''.join(c.get('text','') for c in data.get('content',[]) if c.get('type')=='text'),
                'response':data,'evidence':{'kind':'llm','id':call_id},
                'operation_id':operation_id,'attempt_count':int(headers.get('x-seb-attempt-count',1)),
                'finish_reason':data.get('stop_reason')}

    def item(self,item_id,prompt,grader,**kwargs):
        """Run once through platform retry policy, then grade once; no answer retries.

        grader(text) returns a score in [0,1], or a dict with score, answer_status
        and an optional evidence list. Helper calls must use the same item/budget group.
        A grader API failure leaves the item incomplete without retrying its answer;
        other grader exceptions propagate as program defects, not wrong answers.
        """
        try:
            reply=self.chat(prompt,item_id=item_id,**kwargs)
        except EvaluationError as error:
            execution=('infra_error' if error.category=='infra_error' else
                       'budget_exhausted' if error.category=='budget_exceeded' else 'not_run')
            return {'id':item_id,'score':None,'execution_status':execution,'answer_status':'not_applicable',
                    'evidence':error.evidence,'operation_id':error.operation_id,'error_category':error.category}
        try:
            graded=grader(reply['text']) if reply['text'].strip() else {'score':0,'answer_status':'missing'}
        except EvaluationError as error:
            execution=('infra_error' if error.category=='infra_error' else
                       'budget_exhausted' if error.category=='budget_exceeded' else 'not_run')
            return {'id':item_id,'score':None,'execution_status':execution,'answer_status':'not_applicable',
                    'evidence':[reply['evidence'],*error.evidence],'error_stage':'grading',
                    'answer_operation_id':reply['operation_id'],'operation_id':error.operation_id,
                    'error_category':error.category}
        if not isinstance(graded,dict):graded={'score':graded,'answer_status':'answered'}
        extra=graded.get('evidence',[])
        if not isinstance(extra,list):raise ValueError('Grader evidence must be a list')
        if graded.get('execution_status','completed')!='completed':
            raise ValueError('Return incomplete items explicitly; a grader result must be completed')
        return {**graded,'id':item_id,'execution_status':'completed','evidence':[reply['evidence'],*extra],
                'finish_reason':reply['finish_reason'],'operation_id':reply['operation_id'],
                'attempt_count':reply['attempt_count']}

    def submit(self,path,*,model=None,kind='suite',max_tokens=None,pilot=False,item_id=None,sample_id=None):
        body={'path':str(path),'model':model or self.context.get('model')}
        if sample_id is not None:body['sample_id']=sample_id
        if pilot:body['pilot']=True
        if max_tokens is not None:
            if kind!='agent':raise ValueError('max_tokens applies to agent tasks only')
            body['max_output_tokens']=max_tokens
        return json.loads(self.request('/research/'+('suites' if kind=='suite' else 'agents'),body,item_id=item_id)[0])

    def status(self,job_id):
        return json.loads(self.request('/research/jobs/'+job_id)[0])

    def jobs(self):
        return json.loads(self.request('/research/jobs')[0])

    def cancel(self,job_id):
        return json.loads(self.request('/research/jobs/'+job_id+'/cancel',{})[0])

    def artifacts_list(self,job_id):
        return json.loads(self.request('/research/jobs/'+job_id+'/artifacts')[0])

    def artifact(self,job_id,relative,destination):
        from urllib.parse import quote
        raw,_=self.request('/research/jobs/'+job_id+'/artifacts/'+quote(relative,safe='/'))
        target=Path(destination);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
        return str(target)

    def wait(self,job_id,*,timeout=1800):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            result=self.status(job_id)
            if result['status'] not in ('queued','running'):return result
            time.sleep(2)
        raise TimeoutError('Job still running; poll the same ID, do not resubmit: '+job_id)

    def agent(self,path,*,model=None,timeout=1800,max_tokens=None,item_id=None,sample_id=None):
        job=self.submit(path,model=model,kind='agent',max_tokens=max_tokens,item_id=item_id,sample_id=sample_id)
        result=self.wait(job['id'],timeout=timeout)
        return {'result':result,'evidence':{'kind':'agent','id':job['id']}}

    def suite(self,path,*,model,timeout=1800,sample_id=None):
        job=self.submit(path,model=model,kind='suite',sample_id=sample_id)
        return self.wait(job['id'],timeout=timeout)
