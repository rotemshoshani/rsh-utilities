#!/usr/bin/env python3
"""Install inactive service definitions and visible mode launchers."""
from pathlib import Path
import os
import shutil
import subprocess

home = Path.home()
source = Path(__file__).resolve().parent
units = home / '.config/systemd/user'
visible = home / 'ThinkPad Modes'
state = home / '.local/state/thinkpad-modes'
for folder in (units, visible, state):
    folder.mkdir(parents=True, exist_ok=True)
state.chmod(0o700)
runtime = f'/usr/bin/python3 {source}/modes.py'
common = f'''[Unit]
PartOf=thinkpad-server.target
ConditionPathExists=%h/.local/state/thinkpad-modes/server-enabled

[Service]
WorkingDirectory=%h/projects/meniv/crm
Environment="PATH={home}/.local/npm-global/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin"
UMask=0077
StandardOutput=null
StandardError=null
'''
files = {
    'thinkpad-server.target': '''[Unit]
Description=ThinkPad CRM background services
Wants=thinkpad-error-collector.service thinkpad-bug-worker.timer thinkpad-morning-report.timer

[Install]
WantedBy=default.target
''',
    'thinkpad-error-collector.service': common + '''Type=exec
ExecStart=/usr/bin/node scripts/bug-report-worker/error-collector.mjs
Restart=on-failure
RestartSec=30
TimeoutStopSec=30
''',
}
for name, action, calendar in [('bug-worker', 'worker', '*-*-* 04:00:00 Asia/Jerusalem'),
                               ('morning-report', 'report', '*-*-* 08:00:00 Asia/Jerusalem')]:
    files[f'thinkpad-{name}.service'] = common.replace('[Service]', 'ConditionPathExists=%h/.local/state/thinkpad-modes/schedules-armed\n\n[Service]') + f'''Type=oneshot
ExecStart={runtime} {action}
TimeoutStartSec=infinity
TimeoutStopSec=infinity
'''
    files[f'thinkpad-{name}.timer'] = f'''[Unit]
PartOf=thinkpad-server.target

[Timer]
OnCalendar={calendar}
Persistent=false
AccuracySec=1min
Unit=thinkpad-{name}.service
'''
for name, content in files.items():
    path = units / name
    if path.exists() and path.read_text() != content:
        raise SystemExit(f'Existing different unit left unchanged: {name}')
    path.write_text(content)
for label, action in [('Server Mode', 'server'), ('Laptop Mode', 'laptop')]:
    path = visible / f'{label}.sh'
    command = f'/usr/bin/python3 "{source}/modes.py" {action}'
    if action == 'server':
        command += f' || exit $?\nif [ -t 0 ] && [ -t 1 ]; then\n  exec /usr/bin/python3 "{source}/dashboard.py" --tmux\nfi'
    path.write_text(f'#!/bin/sh\n{command}\n')
    path.chmod(0o755)
dashboard = visible / 'Live Status.sh'
dashboard.write_text(f'#!/bin/sh\nexec /usr/bin/python3 "{source}/dashboard.py" --tmux "$@"\n')
dashboard.chmod(0o755)
shutil.copyfile(source / 'README.md', visible / 'README.md')
installed = state / 'installed-package-lock.json'
if not installed.exists():
    shutil.copyfile(home / 'projects/meniv/crm/package-lock.json', installed)
subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
print('Updated definitions and mode/dashboard launchers; existing service and schedule state preserved.')
