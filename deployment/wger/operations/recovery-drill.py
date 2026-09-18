#!/usr/bin/env python3
"""Restore a verified wger snapshot into disposable Docker resources only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tarfile
import time
import uuid

from snapshot import verify_snapshot

PG = 'docker.io/postgres:15-alpine@sha256:fe0737ba566a2c5b2a28f34433c0a423261900ec17b9bf7ad115e1aae7e57f1b'
WEB = 'ghcr.io/cubatica/fitness-wger:135d8569a3eb27c9f0f74e865d56372421a61294'
PS = 'docker.io/journeyapps/powersync-service@sha256:39f6a534f757afd1c633e91a19b23fde03186d16b5543b4af0f5c8f09d4cb79d'


def run(*args, data=None):
    return subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot')
    args = parser.parse_args()
    source = Path(args.snapshot).resolve()
    verify_snapshot(source)
    name = 'wger-recovery-' + uuid.uuid4().hex[:10]
    work = source.parent / name
    work.mkdir(mode=0o700)
    with tarfile.open(source / 'deployment.tar') as archive:
        archive.extractall(work, filter='data')
    config = {}
    for line in (work / 'config/private.env').read_text().splitlines():
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1); config[key] = value
    database_password = secrets.token_urlsafe(36)
    config.update(POSTGRES_USER='restore', POSTGRES_PASSWORD=database_password, POSTGRES_DB='wger',
                  PS_DATABASE_URI=f'postgres://restore:{database_password}@db:5432/wger',
                  PS_STORAGE_PG_URI=f'postgres://restore:{database_password}@db:5432/wger', PS_PORT='8080')
    env = work / 'recovery.env'; env.write_text('\n'.join(k + '=' + v for k, v in config.items()) + '\n')
    labels = ['--label', 'fitness.backup.recovery-drill=' + name]
    run('docker', 'network', 'create', *labels, '--internal', name)
    for volume in ['db', 'media']:
        run('docker', 'volume', 'create', *labels, name + '-' + volume)
    receipt = {'snapshot': str(source), 'project': name, 'created_containers': [name + '-db', name + '-powersync'],
               'volumes': [name + '-db', name + '-media'], 'networks': [name], 'live_project_untouched': True, 'state': 'starting'}
    receipt_path = work / 'receipt.json'; receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    run('docker', 'run', '-d', '--name', name + '-db', *labels, '--network', name, '--network-alias', 'db',
        '--memory', '384m', '--cpus', '0.25', '--env-file', str(env), '-v', name + '-db:/var/lib/postgresql/data', PG)
    for _ in range(60):
        try:
            run('docker', 'exec', name + '-db', 'pg_isready', '-h', '127.0.0.1', '-U', 'restore'); break
        except subprocess.CalledProcessError:
            time.sleep(1)
    else:
        raise RuntimeError('disposable database did not become ready')
    run('docker', 'exec', '-i', name + '-db', 'pg_restore', '--exit-on-error', '--no-owner', '--no-acl', '-U', 'restore', '-d', 'wger', data=(source / 'database.dump').read_bytes())
    run('docker', 'run', '--rm', '-i', *labels, '--network', 'none', '--memory', '128m', '--cpus', '0.25',
        '--entrypoint', 'tar', '-v', name + '-media:/home/wger/media', WEB, '-C', '/home/wger/media', '-xf', '-', data=(source / 'media.tar').read_bytes())
    run('docker', 'run', '-d', '--name', name + '-powersync', *labels, '--network', name, '--memory', '384m', '--cpus', '0.25',
        '--env-file', str(env), '-e', 'POWERSYNC_CONFIG_PATH=/config/powersync.yaml', '-e', 'PS_JWKS_URL=http://127.0.0.1:9/unavailable',
        '-v', str(work / 'config/powersync.yaml') + ':/config/powersync.yaml:ro',
        '-v', str(work / 'config/sync_rules.yaml') + ':/config/sync_rules.yaml:ro', PS, 'start', '-r', 'unified')
    time.sleep(5)
    if run('docker', 'inspect', '-f', '{{.State.Running}}', name + '-powersync').decode().strip() != 'true':
        raise RuntimeError('restored PowerSync state did not start')
    logs = run('docker', 'logs', name + '-powersync').decode(errors='replace')
    if re.search(r'(?i)(migration|schema).*(error|failed)', logs):
        raise RuntimeError('restored PowerSync state failed compatibility check')
    receipt.update(state='database-media-and-powersync-state-restored-awaiting-independent-app-check', powersync_container_running=True)
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({'receipt': str(receipt_path), 'project': name, 'live_project_untouched': True}))


if __name__ == '__main__':
    main()
