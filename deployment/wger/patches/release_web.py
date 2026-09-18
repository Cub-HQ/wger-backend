#!/usr/bin/env python3
"""Atomically install staged overrides and recreate only the Colima web service."""
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

patch_dir = Path(__file__).resolve().parent
deploy_dir = patch_dir.parent
docker_host = os.environ.get('WGER_DOCKER_HOST', f'unix://{Path.home()}/.colima/default/docker.sock')
writer_lock = os.environ.get('WGER_WRITER_LOCK')
history_lock = os.environ.get('WGER_HISTORY_LOCK')
if not writer_lock or not history_lock:
    raise SystemExit('WGER_WRITER_LOCK and WGER_HISTORY_LOCK are required')
lock_dir = Path(os.environ.get('WGER_RELEASE_LOCK_DIR', deploy_dir / 'locks'))
lock_dir.mkdir(parents=True, exist_ok=True)
locks = []
lock_specs = [(lock_dir / 'web-release.lock', 'flock'), (Path(writer_lock), 'flock'), (Path(history_lock), 'lockf')]
for path, namespace in lock_specs:
    if path != lock_dir / 'web-release.lock' and not path.exists():
        raise SystemExit(f'required writer lock is missing: {path}')
    handle = path.open('a')
    try:
        operation = fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(handle, operation) if namespace == 'flock' else fcntl.lockf(handle, operation)
    except BlockingIOError:
        raise SystemExit(f'writer/import custody is active: {path}') from None
    locks.append(handle)

def run(*args, capture=False):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE if capture else None).stdout.strip() if capture else None

expected_host = f'unix://{Path.home()}/.colima/default/docker.sock'
if docker_host != expected_host or not Path(docker_host.removeprefix('unix://')).is_socket():
    raise SystemExit('Docker is not the reviewed Colima socket')

names = ('react-main.js', 'template.html', 'corresponding-source.json')
for name in names:
    if not (deploy_dir / 'overrides' / f'{name}.next').is_file():
        raise SystemExit(f'missing staged override: {name}.next')

with tempfile.TemporaryDirectory(dir=deploy_dir / 'overrides', prefix='.web-rollback.') as temporary:
    rollback = Path(temporary)
    for name in names:
        current = deploy_dir / 'overrides' / name
        if current.exists():
            shutil.copy2(current, rollback / name)
        shutil.copy2(deploy_dir / 'overrides' / f'{name}.next', current)
    command = ('docker', '-H', docker_host, 'compose', '-f', str(deploy_dir / 'compose.yaml'), 'up', '-d', '--no-deps', '--force-recreate', 'web')
    try:
        run(*command)
    except subprocess.CalledProcessError:
        for name in names:
            current, old = deploy_dir / 'overrides' / name, rollback / name
            if old.exists(): shutil.copy2(old, current)
            else: current.unlink(missing_ok=True)
        run(*command)
        raise SystemExit('web release failed; previous overrides restored and web-only rollback recreated') from None
print('reviewed overrides installed; only the web service was recreated')
