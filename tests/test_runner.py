import base64,json,shutil,subprocess
from pathlib import Path
import pytest
from seb.runner import validate,freeze,correlation
from seb.container import contained


def make_submission(tmp_path):
    root=tmp_path/'submission';root.mkdir()
    shutil.copytree(Path(__file__).parent/'fixtures/arithmetic',root/'tasks/arithmetic')
    (root/'benchmark.json').write_text(json.dumps({'tasks':[{'path':'tasks/arithmetic','weight':1}]}))
    return root


def test_freeze_preserves_exact_inputs(tmp_path):
    root=make_submission(tmp_path);dest=tmp_path/'frozen'
    hashes=freeze(root,dest)
    (root/'tasks/arithmetic/instruction.md').write_text('changed after freeze')
    assert (dest/'tasks/arithmetic/instruction.md').read_text()!='changed after freeze'
    assert 'tasks/arithmetic/instruction.md' in hashes


def test_invalid_paths_weights_and_symlinks(tmp_path):
    root=make_submission(tmp_path)
    (root/'benchmark.json').write_text(json.dumps({'tasks':[{'path':'../outside','weight':1}]}))
    with pytest.raises(ValueError):validate(root)
    (root/'benchmark.json').write_text(json.dumps({'tasks':[{'path':'tasks/arithmetic','weight':-1}]}))
    with pytest.raises(ValueError):validate(root)


def test_correlation_does_not_drop_failures():
    ref=[{'model':'a','target_score':1},{'model':'b','target_score':2}]
    assert correlation([{'model':'a','valid':True,'score':1},{'model':'b','valid':False,'score':None}],ref)['status']=='incomplete'
    assert correlation([{'model':'a','valid':True,'score':1},{'model':'b','valid':True,'score':1}],ref)['spearman'] is None
    assert correlation([{'model':'a','valid':True,'score':2},{'model':'b','valid':True,'score':1}],ref)['spearman']==pytest.approx(-1)


def hook_command(image,trace,command):
    script=Path(__file__).parents[1]/'seb/tool_capture.py'
    event=json.dumps({'tool_use_id':'long-output','tool_name':'Bash','tool_input':{'command':command}})
    binds=[(script,'/opt/capture.py',True),(trace,'/trace',False)]
    p=subprocess.run(contained(image,['/usr/local/bin/python','/opt/capture.py'],binds=binds),input=event,capture_output=True,text=True,timeout=30,env={'PATH':'/usr/bin:/bin'})
    assert p.returncode==0,p.stderr
    return json.loads(p.stdout)['hookSpecificOutput']['updatedInput']['command'],binds


def test_actual_bash_capture_is_not_truncated(tmp_path):
    import os
    image=os.environ.get('SEB_TEST_ROOT')
    if not image:pytest.skip('Set SEB_TEST_ROOT for namespace integration test')
    trace=tmp_path/'trace';trace.mkdir()
    command="python -c \"import sys; sys.stdout.write('X'*1200000); sys.stderr.write('E'*300000)\""
    wrapped,binds=hook_command(image,trace,command)
    p=subprocess.run(contained(image,['/bin/bash','-c',wrapped],binds=binds),capture_output=True,timeout=30,env={'PATH':'/usr/bin:/bin'})
    assert p.returncode==0,p.stderr[:500]
    assert p.stdout==b'X'*1200000
    assert p.stderr==b'E'*300000
    assert (trace/'tools/long-output.stdout').read_bytes()==p.stdout
    assert (trace/'tools/long-output.stderr').read_bytes()==p.stderr


def test_capture_preserves_cwd_exports_and_exit_status(tmp_path):
    import os
    image=os.environ.get('SEB_TEST_ROOT')
    if not image:pytest.skip('Set SEB_TEST_ROOT for namespace integration test')
    trace=tmp_path/'trace';trace.mkdir()
    wrapped,binds=hook_command(image,trace,'cd /tmp; export STATE=preserved; false')
    p=subprocess.run(contained(image,['/bin/bash','-c',wrapped+'; rc=$?; printf "%s:%s:%s" "$PWD" "$STATE" "$rc"'],binds=binds),capture_output=True,timeout=30,env={'PATH':'/usr/bin:/bin'})
    assert p.stdout==b'/tmp:preserved:1'


@pytest.mark.parametrize('ending,status', [('exit 7',7), ('set -e; false',1)])
def test_capture_drains_early_exit_and_keeps_existing_exit_trap(tmp_path,ending,status):
    import os
    image=os.environ.get('SEB_TEST_ROOT')
    if not image:pytest.skip('Set SEB_TEST_ROOT for namespace integration test')
    trace=tmp_path/'trace';trace.mkdir()
    wrapped,binds=hook_command(image,trace,"printf 'before-exit\\n'; "+ending)
    script="trap 'printf \"previous-trap:%s\\n\" \"$?\"' EXIT\n"+wrapped
    p=subprocess.run(contained(image,['/bin/bash','-c',script],binds=binds),capture_output=True,timeout=30,env={'PATH':'/usr/bin:/bin'})
    assert p.returncode==status
    assert p.stdout==f'before-exit\nprevious-trap:{status}\n'.encode()
    assert (trace/'tools/long-output.stdout').read_bytes()==b'before-exit\n'


def test_development_and_evaluation_use_same_effort_and_env(tmp_path,monkeypatch):
    import seb.runner as runner
    root=make_submission(tmp_path)
    captured={}
    def fake_build(environment,destination,cache,logdir):
        destination.mkdir();(destination/'workspace').mkdir()
        return {'cwd':'/app','env':{'TASK_FLAG':'yes'}}
    def fake_launch(*args,**kwargs):
        captured.update(kwargs);return 0
    def fake_verify(args,log,**kwargs):
        (Path(log).parent/'verifier/reward.txt').write_text('1');return 0
    monkeypatch.setattr(runner,'build_task',fake_build)
    monkeypatch.setattr(runner,'launch_claude',fake_launch)
    monkeypatch.setattr(runner,'run_logged',fake_verify)
    result=runner.execute_task(root/'tasks/arithmetic','m','token',{'image_cache':'unused','gateway_socket':'unused','efforts':{'m':'max'}},tmp_path/'run')
    assert result['status']=='ok'
    assert result['effort']=='max'
    assert captured['effort']=='max'
    assert captured['extra_env']=={'TASK_FLAG':'yes'}


def test_task_uses_source_timeouts_and_phase_environment(tmp_path,monkeypatch):
    import seb.runner as runner
    root=make_submission(tmp_path)
    task=root/'tasks/arithmetic'
    (task/'task.toml').write_text('''version = "1.0"
[agent]
timeout_sec = 900
[verifier]
timeout_sec = 1200
[verifier.env]
PHASE = "verify"
[solution.env]
PHASE = "solve"
[environment.env]
COMMON = "both"
''')
    def build(environment,destination,cache,logdir):
        destination.mkdir();return {'cwd':'/app','env':{'IMAGE':'inherited'}}
    captured=[]
    def execute(args,log,**kwargs):
        captured.append((args,kwargs['timeout']))
        if str(log).endswith('verify'):(Path(log).parent/'verifier/reward.txt').write_text('1')
        return 0
    monkeypatch.setattr(runner,'build_task',build);monkeypatch.setattr(runner,'run_logged',execute)
    result=runner.execute_task(task,'oracle','',{'image_cache':'unused'},tmp_path/'run',oracle=True)
    assert result['status']=='ok' and [t for _,t in captured]==[900,1200]
    assert all('both' in args and 'inherited' in args for args,_ in captured)
    assert 'solve' in captured[0][0] and 'verify' in captured[1][0]


def test_docker_arg_scope_inherited_path_and_same_line_env(tmp_path,monkeypatch):
    import seb.container as container
    base=tmp_path/'base';base.mkdir()
    source=tmp_path/'environment';source.mkdir()
    (source/'Dockerfile').write_text('''FROM fixture
ARG BASE_SHA=abc123
ENV PREVIOUS=old
ENV PREVIOUS=new OTHER=$PREVIOUS PATH=/root/go/bin:${PATH}
WORKDIR /app
RUN test "$BASE_SHA" = abc123
CMD ["/bin/bash"]
''')
    monkeypatch.setattr(container,'image_root',lambda *args:base)
    monkeypatch.setattr(container,'image_environment',lambda *args:{'PATH':'/image/bin:/usr/bin'})
    calls=[]
    monkeypatch.setattr(container,'run_logged',lambda args,*a,**k:calls.append(args) or 0)
    result=container.build_task(source,tmp_path/'root','cache',tmp_path/'logs')
    assert result['env']['PATH']=='/root/go/bin:/image/bin:/usr/bin'
    assert result['env']['OTHER']=='old' and result['env']['PREVIOUS']=='new'
    assert 'BASE_SHA' not in result['env'] and 'abc123' in calls[0]
    assert result['default_command']==['/bin/bash']


@pytest.mark.parametrize('harness_error',[False,True])
def test_vnext_wrong_answer_is_scored_but_harness_error_is_not(tmp_path,monkeypatch,harness_error):
    import seb.runner as runner
    from seb.execution_policy import DEFAULT_POLICY
    root=make_submission(tmp_path)
    def build(environment,destination,cache,logdir):
        destination.mkdir();(destination/'workspace').mkdir()
        return {'cwd':'/app','env':{}}
    def launch(*args,**kwargs):
        trace=Path(args[2]);trace.mkdir(parents=True)
        (trace/'claude.stdout').write_text(json.dumps({'type':'result','is_error':harness_error})+'\n')
        assert kwargs['output_tokens']==32768
        return 0
    def verify(args,log,**kwargs):
        (Path(log).parent/'verifier/reward.txt').write_text('0');return 1
    monkeypatch.setattr(runner,'build_task',build)
    monkeypatch.setattr(runner,'launch_claude',launch)
    monkeypatch.setattr(runner,'run_logged',verify)
    result=runner.execute_task(root/'tasks/arithmetic','m','token',
        {'image_cache':'unused','gateway_socket':'unused','evaluation_policy':DEFAULT_POLICY},tmp_path/'run')
    assert result['reward']==0
    assert result['score_status']==('incomplete' if harness_error else 'valid')
    assert result['status']==('execution_failed' if harness_error else 'ok')
