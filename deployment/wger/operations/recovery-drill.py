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
from backup import deployment_services, docker_host, service_bind_mounts



def run(*args, data=None):
    return subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot')
    args = parser.parse_args()
    docker = ('docker', '-H', docker_host())
    source = Path(args.snapshot).resolve()
    verify_snapshot(source)
    name = 'wger-recovery-' + uuid.uuid4().hex[:10]
    work = source.parent / name
    work.mkdir(mode=0o700)
    with tarfile.open(source / 'deployment.tar') as archive:
        archive.extractall(work, filter='data')
    services = deployment_services(work)
    service_bind_mounts(work, services['web'])
    powersync_mounts = sum((('-v', mount) for mount in service_bind_mounts(work, services['powersync'])), ())
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
    run(*docker, 'network', 'create', *labels, '--internal', name)
    for volume in ['db', 'media']:
        run(*docker, 'volume', 'create', *labels, name + '-' + volume)
    receipt = {'snapshot': str(source), 'project': name, 'created_containers': [name + '-db', name + '-powersync'],
               'volumes': [name + '-db', name + '-media'], 'networks': [name], 'live_project_untouched': True, 'state': 'starting'}
    receipt_path = work / 'receipt.json'; receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    run(*docker, 'run', '-d', '--name', name + '-db', *labels, '--network', name, '--network-alias', 'db',
        '--memory', '384m', '--cpus', '0.25', '--env-file', str(env), '-v', name + '-db:/var/lib/postgresql/data', services['db']['image'])
    for _ in range(60):
        try:
            run(*docker, 'exec', name + '-db', 'pg_isready', '-h', '127.0.0.1', '-U', 'restore'); break
        except subprocess.CalledProcessError:
            time.sleep(1)
    else:
        raise RuntimeError('disposable database did not become ready')
    run(*docker, 'exec', '-i', name + '-db', 'pg_restore', '--exit-on-error', '--no-owner', '--no-acl', '-U', 'restore', '-d', 'wger', data=(source / 'database.dump').read_bytes())
    run(*docker, 'run', '--rm', '-i', *labels, '--network', 'none', '--memory', '128m', '--cpus', '0.25',
        '--entrypoint', 'tar', '-v', name + '-media:/home/wger/media', services['web']['image'], '-C', '/home/wger/media', '-xf', '-', data=(source / 'media.tar').read_bytes())
    run(*docker, 'run', '-d', '--name', name + '-powersync', *labels, '--network', name, '--memory', '384m', '--cpus', '0.25',
        '--env-file', str(env), '-e', 'POWERSYNC_CONFIG_PATH=/config/powersync.yaml', '-e', 'PS_JWKS_URL=http://127.0.0.1:9/unavailable',
        *powersync_mounts, services['powersync']['image'], 'start', '-r', 'unified')
    time.sleep(5)
    if run(*docker, 'inspect', '-f', '{{.State.Running}}', name + '-powersync').decode().strip() != 'true':
        raise RuntimeError('restored PowerSync state did not start')
    logs = run(*docker, 'logs', name + '-powersync').decode(errors='replace')
    if re.search(r'(?i)(migration|schema).*(error|failed)', logs):
        raise RuntimeError('restored PowerSync state failed compatibility check')
    receipt.update(state='database-media-and-powersync-state-restored-awaiting-independent-app-check', powersync_container_running=True)
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({'receipt': str(receipt_path), 'project': name, 'live_project_untouched': True}))


if __name__ == '__main__':
    main()
