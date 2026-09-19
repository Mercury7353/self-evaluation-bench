"""Single-container Harbor runner for HPC: OCI extraction + bwrap namespaces.

Uses Apptainer to fetch OCI roots. Dockerfile instructions not implemented are
rejected, never silently ignored. Task/agent output streams are untruncated.
"""
import hashlib, json, os, re, shlex, shutil, signal, subprocess, time, uuid
from pathlib import Path

BASE_ENV={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/root','LANG':'C.UTF-8','TERM':'xterm-256color',
          'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1','MKL_NUM_THREADS':'1',
          'TAR_OPTIONS':'--no-same-owner'}


def contained(root, command, *, cwd='/', env=None, network=False, binds=()):
    args=['bwrap','--bind',str(root),'/', '--unshare-user','--uid','0','--gid','0',
          '--unshare-pid','--unshare-ipc','--unshare-uts','--die-with-parent','--new-session',
          '--proc','/proc','--dev','/dev']
    if not network:args+=['--unshare-net','--cap-add','CAP_NET_ADMIN']
    elif Path('/etc/resolv.conf').exists():args+=['--ro-bind','/etc/resolv.conf','/etc/resolv.conf']
    for source,target,readonly in binds:
        args+=['--ro-bind' if readonly else '--bind',str(source),target]
    for k,v in (BASE_ENV | (env or {})).items():args+=['--setenv',k,str(v)]
    return args+['--chdir',cwd]+command


def run_logged(args, log, *, timeout, cwd=None, env=None, stop_requested=None):
    log=Path(log);log.parent.mkdir(parents=True,exist_ok=True)
    # Gateway credentials are never included in this command argv.
    with log.with_suffix('.stdout').open('wb') as out,log.with_suffix('.stderr').open('wb') as err:
        p=subprocess.Popen(args,stdout=out,stderr=err,cwd=cwd,env=env or BASE_ENV,start_new_session=True)
        info={'pid':p.pid,'started':time.time(),'argv':args,'timeout_seconds':timeout}
        log.with_suffix('.process.json').write_text(json.dumps(info,indent=2))
        reason=None
        deadline=time.monotonic()+timeout
        try:
            while True:
                if p.poll() is not None:
                    rc=p.returncode;break
                if stop_requested:
                    reason=stop_requested()
                    if reason:raise subprocess.TimeoutExpired(args,timeout)
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    reason='timeout';raise subprocess.TimeoutExpired(args,timeout)
                try:
                    rc=p.wait(timeout=min(1,remaining) if stop_requested else remaining)
                    break
                except subprocess.TimeoutExpired:
                    if not stop_requested or time.monotonic()>=deadline:
                        reason='timeout';raise
        except subprocess.TimeoutExpired:
            try:os.killpg(p.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:p.wait(timeout=15)
            except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
            rc=124 if reason=='timeout' else 125
        except BaseException:
            try:os.killpg(p.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:p.wait(timeout=15)
            except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
            info.update(returncode=p.returncode,finished=time.time(),termination_reason='supervisor_error')
            log.with_suffix('.process.json').write_text(json.dumps(info,indent=2))
            raise
        info.update(returncode=rc,finished=time.time(),termination_reason=reason)
        log.with_suffix('.process.json').write_text(json.dumps(info,indent=2))
    return rc


def safe_path(root, path):
    candidate=(Path(root)/str(path).lstrip('/')).resolve()
    if not candidate.is_relative_to(Path(root).resolve()):raise ValueError('Path escapes root')
    return candidate


def image_root(image, cache):
    cache=Path(cache);cache.mkdir(parents=True,exist_ok=True)
    name=hashlib.sha256(image.encode()).hexdigest()[:20]
    dest=cache/name
    if not (dest/'.seb-complete').exists():
        if dest.exists():raise RuntimeError(f'Incomplete image extraction: {dest}')
        rc=run_logged([shutil.which('apptainer') or 'apptainer','build','--fakeroot','--sandbox',str(dest),'docker://'+image],cache/(name+'-build'),timeout=1200,env=dict(os.environ))
        if rc:raise RuntimeError(f'Image build failed; see {cache/name}')
        (dest/'.seb-complete').write_text(image)
    return dest


def image_environment(root):
    """Read OCI ENV inside its namespace, never by sourcing image code on host."""
    script='/.singularity.d/env/10-docker2singularity.sh'
    if not (Path(root)/script.lstrip('/')).is_file():return {}
    proc=subprocess.run(contained(root,['/bin/sh','-ec','. '+script+'; /usr/bin/env -0']),
                        capture_output=True,timeout=30,env=BASE_ENV)
    if proc.returncode:raise RuntimeError('Could not read inherited image environment')
    result={}
    for entry in proc.stdout.split(b'\0'):
        if not entry:continue
        key,sep,value=entry.partition(b'=')
        if not sep:raise ValueError('Invalid image environment output')
        key=key.decode();value=value.decode()
        if key not in ('PWD','SHLVL','_'):result[key]=value
    return result


def docker_expand(value, variables):
    """Expand the simple ARG/ENV forms used in the supplied task Dockerfiles."""
    if '\\$' in value:raise ValueError('Escaped Docker variables require a Docker backend')
    pattern=r'\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))'
    # Reject unsupported expansion syntax rather than execute it on the host.
    if '$' in re.sub(pattern,'',value):raise ValueError('Unsupported Docker variable expansion')
    return re.sub(pattern,lambda match:variables.get(match[1] or match[2],''),value)


def build_task(environment, destination, cache, logdir):
    environment=Path(environment).resolve();destination=Path(destination)
    docker=environment/'Dockerfile'
    if not docker.exists():raise ValueError('An environment/Dockerfile is required')
    for name in ('docker-compose.yaml','docker-compose.yml','compose.yaml'):
        if (environment/name).exists():raise ValueError('Compose requires a Docker backend, unavailable on this HPC host')
    raw=docker.read_text().replace('\\\n',' ')
    lines=[x.strip() for x in raw.splitlines() if x.strip() and not x.lstrip().startswith('#')]
    if not lines or not lines[0].upper().startswith('FROM '):raise ValueError('Dockerfile must start with FROM')
    image=lines[0].split()[1]
    if len(lines[0].split())!=2:raise ValueError('Multistage FROM requires Docker backend')
    root=image_root(image,cache)
    shutil.copytree(root,destination,symlinks=True)
    apt_config=destination/'etc/apt/apt.conf.d'
    if apt_config.is_dir():
        # A single-ID user namespace cannot switch to the image's _apt UID.
        (apt_config/'99seb-user-namespace').write_text('APT::Sandbox::User "root";\n')
    cwd='/';env=image_environment(destination);build_args={};default_command=None
    for index,line in enumerate(lines[1:],1):
        op,_,value=line.partition(' ');op=op.upper();value=value.strip()
        if op=='WORKDIR':
            value=docker_expand(value,{**build_args,**BASE_ENV,**env})
            cwd=os.path.normpath(os.path.join(cwd,value));safe_path(destination,cwd).mkdir(parents=True,exist_ok=True)
        elif op=='ARG':
            fields=shlex.split(value)
            if len(fields)!=1:raise ValueError('ARG requires one name and optional default')
            name,sep,default=fields[0].partition('=')
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',name):raise ValueError('Invalid ARG name')
            if sep:build_args[name]=docker_expand(default,{**build_args,**BASE_ENV,**env})
            else:build_args.setdefault(name,'')
        elif op=='ENV':
            fields=shlex.split(value)
            if not all('=' in x for x in fields):raise ValueError('Use ENV KEY=value syntax')
            previous={**build_args,**BASE_ENV,**env}
            for field in fields:
                k,v=field.split('=',1);env[k]=docker_expand(v,previous)
        elif op=='COPY':
            fields=json.loads(value) if value.startswith('[') else shlex.split(value)
            if len(fields)!=2 or fields[0].startswith('--'):raise ValueError('COPY supports a single context-local source and destination')
            fields=[docker_expand(field,{**build_args,**BASE_ENV,**env}) for field in fields]
            source=(environment/fields[0]).resolve()
            if not source.is_relative_to(environment):raise ValueError('COPY escapes build context')
            target=safe_path(destination,os.path.join(cwd,fields[1]))
            if source.is_dir():shutil.copytree(source,target,dirs_exist_ok=True,symlinks=False)
            else:
                if fields[1].endswith('/') or target.is_dir():target=target/source.name
                target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        elif op=='RUN':
            cmd=json.loads(value) if value.startswith('[') else ['/bin/sh','-ec',value]
            rc=run_logged(contained(destination,cmd,cwd=cwd,env={**build_args,**env},network=True),Path(logdir)/f'build-{index}',timeout=600)
            if rc:raise RuntimeError(f'Dockerfile RUN failed at instruction {index}')
        elif op=='CMD':
            command=json.loads(value) if value.startswith('[') else shlex.split(value)
            if command not in (['bash'],['/bin/bash']):
                raise ValueError('A service CMD requires a backend that starts that service')
            default_command=command
        elif op in ('LABEL','MAINTAINER'):
            continue  # Non-execution metadata; Dockerfile itself is retained.
        else:raise ValueError(f'{op} requires Docker backend; no silent conversion')
    for directory in ('root','workspace','app','opt','run','tmp','trace'):
        (destination/directory).mkdir(exist_ok=True)
    return {'cwd':cwd,'env':env,'base_image':image,'default_command':default_command,
            'namespace_compatibility':{'tar_no_same_owner':True,'apt_sandbox_user':'root'}}


def _launch_claude_once(root, workspace, trace, gateway_socket, token, model, prompt, *, timeout, effort=None, extra_env=None, output_tokens=16384, research_network=False, resume_session=None, extra_binds=(), stop_requested=None):
    trace=Path(trace);trace.mkdir(parents=True,exist_ok=True)
    root=Path(root)
    package=Path(__file__).parent.resolve()
    binary=Path(shutil.which('claude')).resolve()
    cmd=['/opt/claude','-p','--output-format','stream-json','--verbose','--include-partial-messages',
         '--model',model,'--dangerously-skip-permissions','--setting-sources','',
         '--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
         '--tools','Bash,Read,Write,Edit,Glob,Grep','--settings','/run/settings.json']
    if effort:cmd+=['--effort',effort]
    if resume_session:cmd+=['--resume',str(uuid.UUID(resume_session))]
    cmd+=['--',prompt]
    launch={'command':cmd,'cwd':'/workspace','env':{**(extra_env or {}),
        'ANTHROPIC_BASE_URL':'http://127.0.0.1:18765/anthropic','ANTHROPIC_API_KEY':token,
        'ANTHROPIC_MODEL':model,'ANTHROPIC_DEFAULT_SONNET_MODEL':model,
        'ANTHROPIC_DEFAULT_OPUS_MODEL':model,'ANTHROPIC_DEFAULT_HAIKU_MODEL':model,
        'CLAUDE_CODE_SUBAGENT_MODEL':model,'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1',
        'IS_SANDBOX':'1','CLAUDE_CODE_MAX_OUTPUT_TOKENS':str(output_tokens),'DISABLE_AUTOUPDATER':'1',
        'CLAUDE_CONFIG_DIR':'/workspace/.claude','HOME':'/workspace'}}
    if research_network:
        import socket
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        launch.update(enable_loopback=False,proxy_port=port)
        launch['env']['ANTHROPIC_BASE_URL']=f'http://127.0.0.1:{port}/anthropic'
        launch['env']['SEB_GATEWAY_URL']=f'http://127.0.0.1:{port}'
    launchfile=trace/'launch.private.json';launchfile.write_text(json.dumps(launch));launchfile.chmod(0o600)
    capture=trace/'capture-hook.py';shutil.copy2(package/'tool_capture.py',capture)
    settings=trace/'settings.json';settings.write_text(json.dumps({'hooks':{'PreToolUse':[{'matcher':'Bash','hooks':[{'type':'command','command':'/usr/local/bin/python /opt/seb_tool_capture.py'}]}]}}))
    binds=[(workspace,'/workspace',False),(trace,'/trace',False),(gateway_socket,'/run/gateway.sock',False),
           (binary,'/opt/claude',True),(package/'sandbox_entry.py','/opt/seb_entry.py',True),
           (capture,'/opt/seb_tool_capture.py',True),(launchfile,'/run/launch.json',True),(settings,'/run/settings.json',True),*extra_binds]
    args=contained(root,['/usr/local/bin/python','/opt/seb_entry.py'],cwd='/workspace',binds=binds,network=research_network)
    # IS_SANDBOX=1 permits root only inside the fully isolated namespace.
    rc=run_logged(args,trace/'claude',timeout=timeout,stop_requested=stop_requested)
    # Never retain scoped auth in the public artifact manifest.
    launchfile.unlink(missing_ok=True)
    return rc


def launch_claude(root, workspace, trace, gateway_socket, token, model, prompt, *, timeout, continuation_policy=None, **kwargs):
    if continuation_policy:
        from .claude_continuation import launch
        return launch(_launch_claude_once,root,workspace,trace,gateway_socket,token,model,prompt,
                      timeout=timeout,continuation_policy=continuation_policy,**kwargs)
    return _launch_claude_once(root,workspace,trace,gateway_socket,token,model,prompt,timeout=timeout,**kwargs)
