"""SDK executable override; the model's shell lives inside the existing rootfs."""
import json
import os
import sys
from pathlib import Path

from seb.container import contained


def main():
    config = json.loads(Path(os.environ['SEB_CODEX_LAUNCH']).read_text())
    launch = dict(config['container_launch'])
    if sys.argv[1] != 'exec': raise ValueError('Only SDK exec is supported')
    launch['command'] = ['/opt/codex', 'exec', '--ignore-user-config', '--ignore-rules', *sys.argv[2:]]
    path = Path(config['trace']) / 'launch.private.json'
    path.write_text(json.dumps(launch))
    path.chmod(0o600)
    package = Path(__file__).parent
    binds = [(config['workspace'], '/workspace', False),
             (config['trace'], '/trace', False),
             (config['gateway_socket'], '/run/gateway.sock', False),
             (config['binary'], '/opt/codex', True),
             (str(Path(config['binary']).with_name('codex-code-mode-host')), '/opt/codex-code-mode-host', True),
             (package / 'sandbox_entry.py', '/opt/seb_entry.py', True),
             (path, '/run/launch.json', True), *config.get('extra_binds', [])]
    args = contained(config['rootfs'], ['/usr/local/bin/python', '/opt/seb_entry.py'],
                     cwd='/workspace', network=config.get('research_network', False), binds=binds)
    os.execv('/usr/bin/bwrap', args)


if __name__ == '__main__':
    main()
