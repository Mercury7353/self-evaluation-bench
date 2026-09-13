"""Launch one contained Codex SDK researcher, retaining SDK and native traces."""
import hashlib
import json
import shutil
import sys
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


def launch_codex(root, workspace, trace, gateway_socket, designer_token, model, prompt,
                 *, timeout, effort, binary=None, resume_thread=None, extra_env=None, extra_binds=(), stop_requested=None):
    trace = Path(trace).resolve(); trace.mkdir(parents=True, exist_ok=True)
    workspace = Path(workspace).resolve()
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
        'files_sha256':hashes,'isolated_network':True},indent=2))
    prompt_file = trace / 'prompt.txt'; prompt_file.write_text(prompt)
    wrapper = trace / 'codex-wrapper'
    wrapper.write_text(f'#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(repo)!r})\nfrom seb.codex_wrapper import main\nmain()\n')
    wrapper.chmod(0o700)
    config = dict(wrapper=str(wrapper), model=model, effort=effort, binary=str(binary), rootfs=str(root),
                  workspace=str(workspace), trace=str(trace), gateway_socket=str(gateway_socket),
                  baseUrl='http://127.0.0.1:18765/v1', promptFile=str(prompt_file), threadFile=str(trace/'thread.json'),
                  extra_binds=[(str(s),str(t),ro) for s,t,ro in extra_binds],
                  resumeThread=resume_thread, container_launch={
                      'enable_loopback': True, 'proxy_port': 18765, 'cwd': '/workspace',
                      'env': {**(extra_env or {}), 'HOME': '/workspace', 'CODEX_HOME': '/workspace/.codex',
                              'SEB_DESIGNER_TOKEN': designer_token,
                              'PYTHONPATH': (extra_env or {}).get('PYTHONPATH','/workspace'),
                              'SEB_GATEWAY_URL': 'http://127.0.0.1:18765'}})
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
