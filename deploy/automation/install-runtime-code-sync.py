#!/usr/bin/env python3
"""Install the GitHub -> hub -> independent worker code update timer."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('role', choices=['hub', 'dbb3', 'wsl', 'hk'])
args = parser.parse_args()
assert os.geteuid() == 0
source = Path(__file__).with_name('sync-runtime-code.py')
spec = importlib.util.spec_from_file_location('runtime_updater', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
node = module.NODES[args.role]
python = Path(node['root']) / node['environment'] / 'bin/python'
assert python.is_file()
destination = Path('/usr/local/lib/hermes-runtime-sync')
destination.mkdir(parents=True, exist_ok=True)
assert destination.resolve() == destination
installed = destination / 'sync-runtime-code.py'
if installed.exists():
    shutil.copy2(installed, destination / ('sync-runtime-code.before-' + str(time.time_ns()) + '.py'))
shutil.copy2(source, installed)
installed.chmod(0o755)
service = Path('/etc/systemd/system/hermes-runtime-sync.service')
timer = Path('/etc/systemd/system/hermes-runtime-sync.timer')
service.write_text(f'''[Unit]
Description=Synchronize Hermes code from the approved GitHub release ({args.role})
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
ExecStart={python} {installed} {args.role}
TimeoutStartSec=20min
TimeoutStopSec=30s
KillMode=control-group
UMask=0022
''')
timer.write_text('''[Unit]
Description=Check the committed Hermes runtime release

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
RandomizedDelaySec=10s
Persistent=true
Unit=hermes-runtime-sync.service

[Install]
WantedBy=timers.target
''')
for path in [service, timer]:
    path.chmod(0o644)
previous = 'hermes-git-update.timer' if args.role == 'hub' else 'hermes-fabric-update.timer'
subprocess.run(['systemctl', 'disable', '--now', previous], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'enable', '--now', timer.name], check=True)
print(json.dumps({'role': args.role, 'timer': timer.name, 'source': 'github.com/given33/hermes-agent',
                  'previous_timer': previous, 'instance_homes_unchanged': True}))
