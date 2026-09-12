"""PreToolUse Bash hook: tee full outputs without changing shell state.

The brace group runs in the original shell, preserving cd/export semantics.
Wait for the two capture processes before returning the command status, so fast
commands cannot finish before Claude has received their output.
"""
import json
import shlex
import sys
import uuid
from pathlib import Path

root=Path('/trace/tools');root.mkdir(parents=True,exist_ok=True)
event=json.load(sys.stdin)
call=event.get('tool_use_id') or uuid.uuid4().hex
call=''.join(c for c in call if c.isalnum() or c in '-_')
(root/(call+'.hook.json')).write_text(json.dumps(event,ensure_ascii=False))
if event.get('tool_name')=='Bash':
    inputs=dict(event['tool_input']);original=inputs['command']
    (root/(call+'.command')).write_text(original)
    out=shlex.quote(str(root/(call+'.stdout')))
    err=shlex.quote(str(root/(call+'.stderr')))
    prefix='__seb_capture_'+uuid.uuid4().hex
    ofd,efd,opid,epid,rc=(prefix+s for s in ('_out','_err','_out_pid','_err_pid','_rc'))
    cleanup,previous,on_exit=(prefix+s for s in ('_cleanup','_previous_exit','_on_exit'))
    inputs['command']='\n'.join([
        f'{previous}=$(trap -p EXIT)',
        f'exec {{{ofd}}}> >(/usr/bin/tee {out})', f'{opid}=$!',
        f'exec {{{efd}}}> >(/usr/bin/tee {err} >&2)', f'{epid}=$!',
        f'{cleanup}() {{',
        f'exec {{{ofd}}}>&- {{{efd}}}>&-',
        f'wait "${opid}" "${epid}" || :', '}',
        f'{on_exit}() {{', f'local {rc}=$?', cleanup, 'trap - EXIT',
        f'if [[ -n "${previous}" ]]; then',
        f'eval "set -- ${previous}"',
        f'if (exit "${rc}"); then eval "$3"; else eval "$3"; fi',
        'fi', f'return "${rc}"', '}',
        f'trap {shlex.quote(on_exit)} EXIT',
        '{', original, f'}} >&"${ofd}" 2>&"${efd}"', f'{rc}=$?',
        cleanup, 'trap - EXIT',
        f'if [[ -n "${previous}" ]]; then eval "${previous}"; fi',
        f'unset -f {cleanup} {on_exit}',
        f'(exit "${rc}")',
    ])
    print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse','updatedInput':inputs}}))
