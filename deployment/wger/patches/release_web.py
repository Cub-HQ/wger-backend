#!/usr/bin/env python3
"""Install the reviewed fork with database-safe rollback to the prior image and schema."""
import fcntl
from release_env import read_database_environment
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

patch_dir = Path(__file__).resolve().parent
deploy_dir = Path(os.environ.get('WGER_DEPLOY_DIR', patch_dir.parent)).resolve()
docker_host = os.environ.get('WGER_DOCKER_HOST', f'unix://{Path.home()}/.colima/default/docker.sock')
writer_lock = os.environ.get('WGER_WRITER_LOCK')
history_lock = os.environ.get('WGER_HISTORY_LOCK')
if not writer_lock or not history_lock:
    raise SystemExit('WGER_WRITER_LOCK and WGER_HISTORY_LOCK are required')
lock_dir = Path(os.environ.get('WGER_RELEASE_LOCK_DIR', deploy_dir / 'locks'))
lock_dir.mkdir(parents=True, exist_ok=True)
locks = []
for path, namespace in [(lock_dir / 'web-release.lock', 'flock'), (Path(writer_lock), 'flock'), (Path(history_lock), 'lockf')]:
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
    result = subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def stream_to(path, *args):
    with path.open('wb') as output:
        subprocess.run(args, check=True, stdout=output)


def stream_from(path, *args):
    with path.open('rb') as source:
        subprocess.run(args, check=True, stdin=source)


def compose(*args, capture=False, files=()):
    command = ['docker', '-H', docker_host, 'compose', '-f', str(deploy_dir / 'compose.yaml')]
    for file in files:
        command.extend(('-f', str(file)))
    return run(*command, *args, capture=capture)


def wait_healthy(container, attempts=60):
    for _ in range(attempts):
        state = run('docker', '-H', docker_host, 'inspect', '-f', '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}', container, capture=True)
        if state == 'healthy':
            return
        if state in {'exited', 'dead'}:
            break
        time.sleep(1)
    raise subprocess.CalledProcessError(1, ('healthcheck', container))


expected_host = f'unix://{Path.home()}/.colima/default/docker.sock'
if docker_host != expected_host or not Path(docker_host.removeprefix('unix://')).is_socket():
    raise SystemExit('Docker is not the reviewed Colima socket')

names = ('react-main.js', 'template.html', 'corresponding-source.json')
for name in names:
    if not (deploy_dir / 'overrides' / f'{name}.next').is_file():
        raise SystemExit(f'missing staged override: {name}.next')

services = ('web', 'celery_worker', 'celery_beat')
private_env = deploy_dir / 'config/private.env'
if not private_env.is_file():
    raise SystemExit('missing private release environment')
try:
    database_environment = read_database_environment(private_env)
except ValueError as error:
    raise SystemExit(str(error)) from None
db_user = database_environment['POSTGRES_USER']
db_name = database_environment['POSTGRES_DB']
container_ids = compose('ps', '-q', *services, capture=True).splitlines()
if len(container_ids) != len(services):
    raise SystemExit('web and both workers must be running before release')
powersync_id = compose('ps', '-q', 'powersync', capture=True)
if not powersync_id:
    raise SystemExit('PowerSync must be running before release')
prior_powersync_image = run('docker', '-H', docker_host, 'inspect', '-f', '{{.Image}}', powersync_id, capture=True)
prior_powersync_state = run('docker', '-H', docker_host, 'inspect', '-f', '{{.State.Status}}', powersync_id, capture=True)
if prior_powersync_state != 'running':
    raise SystemExit('PowerSync must be running before release')
prior_images = {service: run('docker', '-H', docker_host, 'inspect', '-f', '{{.Image}}', container, capture=True) for service, container in zip(services, container_ids)}
prior_schema = compose('exec', '-T', 'web', 'python3', 'manage.py', 'showmigrations', '--plan', capture=True)
compose('build', *services)

with tempfile.TemporaryDirectory(dir=deploy_dir / 'overrides', prefix='.web-rollback.') as temporary:
    rollback = Path(temporary)
    database_backup = rollback / 'database.dump'
    rollback_images = {**prior_images, 'powersync': prior_powersync_image}
    rollback_override = rollback / 'compose.rollback.yaml'
    rollback_override.write_text('services:\n' + ''.join(f'  {service}:\n    image: fitness-wger-rollback:{service}\n    build: null\n' for service in rollback_images))
    for service, image in rollback_images.items():
        run('docker', '-H', docker_host, 'tag', image, f'fitness-wger-rollback:{service}')
    for name in names:
        current = deploy_dir / 'overrides' / name
        if current.exists():
            shutil.copy2(current, rollback / name)
    snapshot_complete = False
    try:
        compose('stop', 'powersync', *services)
        stream_to(database_backup, 'docker', '-H', docker_host, 'compose', '-f', str(deploy_dir / 'compose.yaml'), 'exec', '-T', 'db', 'pg_dump', '-Fc', '-U', db_user, db_name)
        snapshot_complete = True
        for name in names:
            shutil.copy2(deploy_dir / 'overrides' / f'{name}.next', deploy_dir / 'overrides' / name)
        compose('up', '-d', '--no-deps', '--force-recreate', *services)
        compose('exec', '-T', 'web', 'python3', 'manage.py', 'migrate', '--no-input')
        wait_healthy(compose('ps', '-q', 'web', capture=True))
        logical_bundle = 'node/@wger-project/react-components/build/main.js'
        manifest_lookup = f'from django.contrib.staticfiles.storage import staticfiles_storage; print(staticfiles_storage.stored_name({logical_bundle!r}))'
        served_name = compose('exec', '-T', 'web', 'python3', 'manage.py', 'shell', '-c', manifest_lookup, capture=True).splitlines()[-1]
        served_path = Path(served_name)
        if served_path.parent.as_posix() != 'node/@wger-project/react-components/build' or served_path.name == 'main.js' or not (served_path.name.startswith('main.') and served_path.suffix == '.js'):
            raise subprocess.CalledProcessError(1, ('verify', 'browser-bundle-manifest'))
        with tempfile.NamedTemporaryFile() as served_bundle:
            compose('cp', f'nginx:/wger/static/{served_path.as_posix()}', served_bundle.name)
            expected = (deploy_dir / 'overrides' / 'react-main.js').read_bytes()
            actual = Path(served_bundle.name).read_bytes()
        map_marker = b'sourceMappingURL=main.js.map'
        if re.sub(rb'sourceMappingURL=main\.js\.[0-9a-f]{12}\.map', map_marker, actual) != expected:
            raise subprocess.CalledProcessError(1, ('verify', 'browser-bundle'))
        compose('up', '-d', '--no-deps', '--force-recreate', 'powersync')
        resumed = compose('ps', '-q', 'powersync', capture=True)
        resumed_image = run('docker', '-H', docker_host, 'inspect', '-f', '{{.Image}}', resumed, capture=True)
        resumed_state = run('docker', '-H', docker_host, 'inspect', '-f', '{{.State.Status}}', resumed, capture=True)
        if resumed_image != prior_powersync_image or resumed_state != prior_powersync_state:
            raise subprocess.CalledProcessError(1, ('resume', 'powersync'))
    except (subprocess.CalledProcessError, OSError):
        for name in names:
            current, old = deploy_dir / 'overrides' / name, rollback / name
            if old.exists(): shutil.copy2(old, current)
            else: current.unlink(missing_ok=True)
        compose('stop', 'powersync', *services)
        if snapshot_complete:
            compose('exec', '-T', 'db', 'dropdb', '--if-exists', '--force', '-U', db_user, db_name)
            compose('exec', '-T', 'db', 'createdb', '-U', db_user, db_name)
            stream_from(database_backup, 'docker', '-H', docker_host, 'compose', '-f', str(deploy_dir / 'compose.yaml'), 'exec', '-T', 'db', 'pg_restore', '--exit-on-error', '--no-owner', '--no-acl', '-U', db_user, '-d', db_name)
        compose('up', '-d', '--no-deps', '--force-recreate', *services, files=(rollback_override,))
        wait_healthy(compose('ps', '-q', 'web', capture=True, files=(rollback_override,)))
        restored_schema = compose('exec', '-T', 'web', 'python3', 'manage.py', 'showmigrations', '--plan', capture=True, files=(rollback_override,))
        if snapshot_complete and restored_schema != prior_schema:
            raise SystemExit('rollback restored services but not the prior database schema')
        compose('up', '-d', '--no-deps', '--force-recreate', 'powersync', files=(rollback_override,))
        resumed = compose('ps', '-q', 'powersync', capture=True, files=(rollback_override,))
        resumed_image = run('docker', '-H', docker_host, 'inspect', '-f', '{{.Image}}', resumed, capture=True)
        resumed_state = run('docker', '-H', docker_host, 'inspect', '-f', '{{.State.Status}}', resumed, capture=True)
        if resumed_image != prior_powersync_image or resumed_state != prior_powersync_state:
            raise SystemExit('rollback restored the app but not exact PowerSync image/state')
        raise SystemExit('fork release failed; prior writer states, overrides, database schema/data and exact images restored') from None
print('reviewed fork image installed; closed-window migrations completed and all writers resumed')
