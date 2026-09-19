"""Launch one contained Codex SDK researcher, retaining SDK and native traces."""
import hashlib
import json
import shutil
import socket
import sys
import time
from pathlib import Path

from .container import run_logged


def runtime_files(binary=None):
    harness=Path(__file__).resolve().parent.parent/'harness/codex'
    binary=Path(binary or harness/'node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex').resolve()
    if not shutil.which('node') or not (harness/'node_modules/@openai/codex-sdk').is_dir():
        raise FileNotFoundError('Install Node and pinned harness/codex dependencies with npm ci')
    if not binary.is_file() or not binary.with_name('codex-code-mode-host').is_file():
        raise FileNotFoundError('Install the pinned Codex CLI and its code-mode host')
    return harness,binary


def _launch_codex_once(root, workspace, trace, gateway_socket, designer_token, model, prompt,
                 *, timeout, effort, binary=None, resume_thread=None, extra_env=None, extra_binds=(), stop_requested=None, research_network=False):
    trace = Path(trace).resolve(); trace.mkdir(parents=True, exist_ok=True)
    workspace = Path(workspace).resolve()
    # Keep async I/O pools within shared HPC thread quotas. This does not cap
    # shell CPU affinity, model reasoning, or the candidate execution policy.
    runtime_threads='4'
    (workspace/'.codex').mkdir(exist_ok=True)
    repo = Path(__file__).resolve().parent.parent
    harness,binary = runtime_files(binary)
    files=[harness/'package-lock.json',harness/'run.mjs',harness/'outcome.mjs',
           binary,binary.with_name('codex-code-mode-host')]
    hashes={}
    for path in files:
        with path.open('rb') as stream:hashes[str(path)]=hashlib.file_digest(stream,'sha256').hexdigest()
    (trace/'runtime.json').write_text(json.dumps({'model':model,'effort':effort,
        'sdk_version':json.loads((harness/'node_modules/@openai/codex-sdk/package.json').read_text())['version'],
        'files_sha256':hashes,'isolated_network':not research_network,'tokio_worker_threads':int(runtime_threads)},indent=2))
    port = 18765
    if research_network:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
    base_url = f'http://127.0.0.1:{port}'
    prompt_file = trace / 'prompt.txt'; prompt_file.write_text(prompt)
    wrapper = trace / 'codex-wrapper'
    wrapper.write_text(f'#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(repo)!r})\nfrom seb.codex_wrapper import main\nmain()\n')
    wrapper.chmod(0o700)
    config = dict(wrapper=str(wrapper), model=model, effort=effort, binary=str(binary), rootfs=str(root),
                  workspace=str(workspace), trace=str(trace), gateway_socket=str(gateway_socket),
                  baseUrl=base_url+'/v1', research_network=research_network, promptFile=str(prompt_file), threadFile=str(trace/'thread.json'),
                  extra_binds=[(str(s),str(t),ro) for s,t,ro in extra_binds],
                  resumeThread=resume_thread, container_launch={
                      'enable_loopback': not research_network, 'proxy_port': port, 'cwd': '/workspace',
                      'env': {**(extra_env or {}), 'HOME': '/workspace', 'CODEX_HOME': '/workspace/.codex',
                              'TOKIO_WORKER_THREADS':runtime_threads,
                              'SEB_DESIGNER_TOKEN': designer_token,
                              'PYTHONPATH': (extra_env or {}).get('PYTHONPATH','/workspace'),
                              'SEB_GATEWAY_URL': base_url}})
    if resume_thread and not any((workspace/'.codex/sessions').rglob('*'+resume_thread+'*.jsonl')):
        raise ValueError('Requested resume thread is absent from the isolated workspace')
    private = trace / 'sdk.private.json'
    private.write_text(json.dumps(config)); private.chmod(0o600)
    try:
        return run_logged([shutil.which('node'), str(harness/'run.mjs'), str(private)],
                          trace/'codex', timeout=timeout, stop_requested=stop_requested)
    finally:
        private.unlink(missing_ok=True)
        (trace/'launch.private.json').unlink(missing_ok=True)


def transport_failure(trace):
    """Only terminal transport events qualify; never inspect model/tool prose."""
    messages=[]
    path=Path(trace)/'codex.stdout'
    if path.exists():
        for line in path.read_text().splitlines():
            try:event=json.loads(line)
            except ValueError:continue
            if event.get('type')=='error':messages.append(event.get('message',''))
            elif event.get('type')=='turn.failed':messages.append((event.get('error') or {}).get('message',''))
    text=' '.join(messages).lower()
    if any(x in text for x in ('policy','quota','unauthorized','authentication','budget','rate limit','403','401','429')):
        return False
    return any(x in text for x in ('stream disconnected','connection reset','connection closed','error decoding response body','network error'))


def launch_codex(root, workspace, trace, gateway_socket, designer_token, model, prompt,
                 *, timeout, effort, binary=None, resume_thread=None, extra_env=None,
                 extra_binds=(), stop_requested=None, continuation_policy=None, research_network=False):
    if not continuation_policy:
        return _launch_codex_once(root,workspace,trace,gateway_socket,designer_token,model,prompt,
            timeout=timeout,effort=effort,binary=binary,resume_thread=resume_thread,
            extra_env=extra_env,extra_binds=extra_binds,stop_requested=stop_requested,research_network=research_network)
    trace=Path(trace);trace.mkdir(parents=True,exist_ok=True)
    deadline=time.monotonic()+timeout
    threshold=continuation_policy['reprompt_remaining_seconds']
    retries=continuation_policy['transport_retries'];failures=0;records=[]
    initial_prompt=prompt;current_prompt=prompt
    while True:
        remaining=deadline-time.monotonic()
        attempt=trace/f'attempt-{len(records)+1:03d}'
        rc=_launch_codex_once(root,workspace,attempt,gateway_socket,designer_token,model,current_prompt,
            timeout=max(0.001,remaining),effort=effort,binary=binary,resume_thread=resume_thread,
            extra_env=extra_env,extra_binds=extra_binds,stop_requested=stop_requested,research_network=research_network)
        # The parent is a documented final-attempt projection for existing consumers.
        # Every prior attempt, including failed terminal events, remains immutable.
        for name in ('codex.stdout','codex.stderr','codex.process.json','thread.json','runtime.json'):
            src=attempt/name
            if src.exists():shutil.copyfile(src,trace/name)
        (trace/'prompt.txt').write_text(initial_prompt)
        session=attempt/'thread.json'
        if session.exists():resume_thread=json.loads(session.read_text())['thread_id']
        remaining=deadline-time.monotonic()
        stopped=stop_requested() if stop_requested else None
        reason='finished';delay=0
        if not stopped and resume_thread and remaining>0:
            if rc==0 and remaining>=threshold:
                reason='early_return';delay=1
            elif rc!=0 and transport_failure(attempt) and failures<retries:
                failures+=1;reason='transport_recovery';delay=min(30,2**failures)
        records.append({'attempt':str(attempt),'returncode':rc,'session_id':resume_thread,
            'remaining_seconds':remaining,'decision':reason,'stop_reason':stopped,'at':time.time()})
        (trace/'continuations.json').write_text(json.dumps({'policy':continuation_policy,
            'projection':'Parent trace mirrors final attempt; complete history in attempt directories.',
            'attempts':records},indent=2))
        if reason=='finished':return rc
        time.sleep(min(delay,max(0,remaining)))
        if deadline-time.monotonic()<=0:return rc
        current_prompt=(f'You still have {int(deadline-time.monotonic())} seconds remaining in the original research window. '
            'Please continue improving your benchmark and maximize its evaluation quality. '
            'Continue this same session and workspace with the original wallets and deadline. '
            'Inspect existing test job IDs and saved responses; do not repeat completed wrong or empty answers. '
            'Choose your own research methods and testing schedule. '
            +('The preceding turn was interrupted by a transport failure; reconcile pending operations before issuing new work.'
              if reason=='transport_recovery' else ''))
