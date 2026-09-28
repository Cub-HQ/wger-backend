#!/usr/bin/env python3
"""Remove only disposable wger recovery resources named in their receipt."""

import argparse
import json
from pathlib import Path
import re
import subprocess
from backup import docker_host

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('receipt')
args = parser.parse_args()
DOCKER = ['docker', '-H', docker_host()]
path = Path(args.receipt)
receipt = json.loads(path.read_text())
name = receipt['project']
if not re.fullmatch(r'wger-recovery-[0-9a-f]{10}', name):
    raise ValueError('not a recovery drill project')
for kind, identifiers in [('container', receipt['created_containers']), ('volume', receipt['volumes']), ('network', receipt['networks'])]:
    existing = subprocess.check_output([*DOCKER, kind, 'ls'] + (['-a'] if kind == 'container' else []) +
                                       ['--format', '{{.Names}}' if kind == 'container' else '{{.Name}}'], text=True).splitlines()
    for identifier in identifiers:
        if identifier not in existing:
            continue
        obj = json.loads(subprocess.check_output([*DOCKER, kind, 'inspect', identifier]))[0]
        labels = obj.get('Config', {}).get('Labels', {}) if kind == 'container' else obj.get('Labels', {})
        if labels.get('fitness.backup.recovery-drill') != name:
            raise ValueError('recovery resource ownership mismatch')
for identifier in reversed(receipt['created_containers']):
    subprocess.run([*DOCKER, 'rm', '-f', identifier], check=False, stdout=subprocess.DEVNULL)
for identifier in receipt['volumes']:
    subprocess.run([*DOCKER, 'volume', 'rm', identifier], check=False, stdout=subprocess.DEVNULL)
for identifier in receipt['networks']:
    subprocess.run([*DOCKER, 'network', 'rm', identifier], check=False, stdout=subprocess.DEVNULL)
receipt['state'] = 'recovery drill resources cleaned; snapshot retained'
path.write_text(json.dumps(receipt, indent=2) + '\n')
print(receipt['state'])
