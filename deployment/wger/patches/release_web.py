#!/usr/bin/env python3
"""Install reviewed compose assets with database-safe rollback to prior images and schema."""
import fcntl
from release_env import read_database_environment
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import hashlib
import json
from urllib.parse import urljoin
from urllib.request import Request, urlopen
import signal


def interrupted(signum, frame):
    raise OSError('release interrupted')


signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)

source_deploy = Path(os.environ['WGER_SOURCE_DEPLOY']).resolve() if os.environ.get('WGER_SOURCE_DEPLOY') else None
config_names = ('compose.yaml', 'overrides/settings-main.py', 'overrides/manager-urls.py', 'config/nginx.conf', 'config/powersync.yaml', 'config/sync_rules.yaml', 'formats/en_AU/formats.py')
public_url = os.environ.get('WGER_PUBLIC_URL', 'https://gym.tailnet.invalid:8098').rstrip('/')


def http_bundle(expected):
    with urlopen(Request(public_url + '/en-au/user/login', headers={'Cache-Control': 'no-cache'}), timeout=30) as response:
        html = response.read().decode('utf-8')
    names = re.findall(r'''(?:src=["'])([^"']*/static/node/@wger-project/react-components/build/main\.[0-9a-f]+\.js)(?:["'])''', html)
    if not names:
        raise OSError('public login does not reference a hashed browser bundle')
    url = urljoin(public_url + '/', names[-1])
    if not url.startswith(public_url + '/'):
        raise OSError('browser bundle escaped public origin')
    with urlopen(Request(url, headers={'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity'}), timeout=60) as response:
        actual = response.read()
        date, modified = response.headers.get('Date'), response.headers.get('Last-Modified')
    normalized = re.sub(rb'sourceMappingURL=main\.js\.[0-9a-f]{12}\.map', b'sourceMappingURL=main.js.map', actual)
    if normalized != expected or not date or not modified:
        raise OSError('public browser bundle/date does not match staged release')
    digest = hashlib.sha256(expected).hexdigest()
    if os.environ.get('WGER_EXPECTED_SHA256', digest) != digest:
        raise OSError('staged bundle changed after preparation')
    return {'url': url, 'date': date, 'last_modified': modified, 'expected_sha256': digest,
            'served_sha256': hashlib.sha256(actual).hexdigest(), 'normalized_sha256': hashlib.sha256(normalized).hexdigest(),
            'markers': {marker: marker.encode() in actual for marker in ('muscular_system_back.svg', 'sourceMappingURL=')}}

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
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(f'command failed ({result.returncode}): {result.stderr.strip()}')
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


def setup_powersync_storage():
    # Use reviewed release machinery, not the possibly older restored image's command.
    stream_from(patch_dir / 'setup-powersync-storage.py', 'docker', '-H', docker_host,
                'compose', '-f', str(deploy_dir / 'compose.yaml'), 'exec', '-T', 'web',
                'python3', 'manage.py', 'shell', '-c',
                'import sys; exec(sys.stdin.read()); Command().handle(schema=Command.DEFAULT_SCHEMA)')


def rendered_config(path, files=()):
    command = ['docker', '-H', docker_host, 'compose', '--project-directory', str(deploy_dir), '-f', str(path)]
    for file in files:
        command.extend(('-f', str(file)))
    return json.loads(run(*command, 'config', '--format', 'json', capture=True))


def verify_binds(config, staged=False):
    for service in config['services'].values():
        for mount in service.get('volumes', []):
            if mount['type'] != 'bind':
                continue
            path = Path(mount['source'])
            # This compose uses named volumes for directories; every bind is a file.
            if staged and source_deploy and path.is_relative_to(deploy_dir):
                relative = path.relative_to(deploy_dir).as_posix()
                if relative in config_names:
                    path = source_deploy / relative
                elif relative in {'overrides/' + name for name in names}:
                    path = source_deploy / (relative + '.next')
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents) or not path.is_file():
                raise RuntimeError('bind source missing or wrong type: ' + str(path))


def wait_healthy(container, attempts=450):
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

names = ('react-main.js', 'template.html', 'history-overview.html', 'api-key.html', 'pdf.py', 'corresponding-source.json')
for name in names:
    if not ((source_deploy or deploy_dir) / 'overrides' / f'{name}.next').is_file():
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
# Non-web service/topology changes have no reviewed activation in this route.
before_config = rendered_config(deploy_dir / 'compose.yaml')
verify_binds(before_config)
if source_deploy:
    next_config = rendered_config(source_deploy / 'compose.yaml')
    verify_binds(next_config, staged=True)
    for key in ('name', 'volumes', 'networks'):
        if before_config.get(key) != next_config.get(key):
            raise SystemExit('DEPLOY_MISSING: compose topology change requires a reviewed route')
    if set(before_config['services']) != set(next_config['services']):
        raise SystemExit('DEPLOY_MISSING: service set changed')
    for service in ('db', 'cache', 'nginx'):
        if before_config['services'][service] != next_config['services'][service]:
            raise SystemExit('DEPLOY_MISSING: unsupported service change: ' + service)

with tempfile.TemporaryDirectory(dir=deploy_dir / 'overrides', prefix='.web-rollback.') as temporary:
    rollback = Path(temporary)
    database_backup = rollback / 'database.dump'
    rollback_images = {**prior_images, 'powersync': prior_powersync_image}
    rollback_override = rollback / 'compose.rollback.yaml'
    rollback_override.write_text('services:\n' + ''.join(f'  {service}:\n    image: fitness-wger-rollback:{service}\n' for service in rollback_images))
    for service, image in rollback_images.items():
        run('docker', '-H', docker_host, 'tag', image, f'fitness-wger-rollback:{service}')
    verify_binds(rendered_config(deploy_dir / 'compose.yaml', (rollback_override,)))
    for name in names:
        current = deploy_dir / 'overrides' / name
        if current.exists():
            shutil.copy2(current, rollback / name)
    snapshot_complete = False
    previous_config = {}
    if source_deploy:
        for name in config_names:
            if any(parent.is_symlink() for parent in (deploy_dir / name).parents):
                raise SystemExit('DEPLOY_MISSING: symlink deployment ancestor')
            current = deploy_dir / name
            if current.is_symlink():
                raise SystemExit('DEPLOY_MISSING: symlink deployment target')
            if current.parent.exists() and not current.parent.is_dir():
                raise SystemExit('DEPLOY_MISSING: deployment parent is not a directory')
            previous_config[name] = current.read_bytes() if current.exists() else None
    try:
        compose('stop', 'powersync', *services)
        stream_to(database_backup, 'docker', '-H', docker_host, 'compose', '-f', str(deploy_dir / 'compose.yaml'), 'exec', '-T', 'db', 'pg_dump', '-Fc', '-U', db_user, db_name)
        snapshot_complete = True
        if source_deploy:
            for name in config_names:
                current = deploy_dir / name
                current.parent.mkdir(parents=True, exist_ok=True)
                # Preserve mounted-file inode when one exists; never copy private.env or private directories.
                shutil.copyfile(source_deploy / name, current)
            compose('config', '--quiet')
        for name in names:
            shutil.copy2((source_deploy or deploy_dir) / 'overrides' / f'{name}.next', deploy_dir / 'overrides' / name)
        compose('up', '-d', '--no-build', '--no-deps', '--force-recreate', *services)
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
        compose('up', '-d', '--no-build', '--no-deps', '--force-recreate', 'powersync')
        resumed = compose('ps', '-q', 'powersync', capture=True)
        resumed_image = run('docker', '-H', docker_host, 'inspect', '-f', '{{.Image}}', resumed, capture=True)
        resumed_state = run('docker', '-H', docker_host, 'inspect', '-f', '{{.State.Status}}', resumed, capture=True)
        if resumed_image != prior_powersync_image or resumed_state != prior_powersync_state:
            raise subprocess.CalledProcessError(1, ('resume', 'powersync'))
        # nginx resolves upstream addresses at reload, after recreated services exist.
        compose('exec', '-T', 'nginx', 'nginx', '-t')
        compose('exec', '-T', 'nginx', 'nginx', '-s', 'reload')
        live_proof = http_bundle(expected)
    except Exception as release_error:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            for name, content in previous_config.items():
                current = deploy_dir / name
                if content is None:
                    if current.is_dir() and not current.is_symlink() and current.stat().st_uid == os.getuid():
                        current.rmdir()
                    else:current.unlink(missing_ok=True)
                else:current.write_bytes(content)
            for name in names:
                current, old = deploy_dir / 'overrides' / name, rollback / name
                if old.exists(): shutil.copy2(old, current)
                else: current.unlink(missing_ok=True)
            verify_binds(rendered_config(deploy_dir / 'compose.yaml', (rollback_override,)))
            compose('stop', 'powersync', *services)
            if snapshot_complete:
                compose('exec', '-T', 'db', 'dropdb', '--if-exists', '--force', '-U', db_user, db_name)
                compose('exec', '-T', 'db', 'createdb', '-U', db_user, db_name)
                stream_from(database_backup, 'docker', '-H', docker_host, 'compose', '-f', str(deploy_dir / 'compose.yaml'), 'exec', '-T', 'db', 'pg_restore', '--exit-on-error', '--no-owner', '--no-acl', '-U', db_user, '-d', db_name)
            compose('up', '-d', '--no-build', '--no-deps', '--force-recreate', *services, files=(rollback_override,))
            wait_healthy(compose('ps', '-q', 'web', capture=True, files=(rollback_override,)))
            restored_schema = compose('exec', '-T', 'web', 'python3', 'manage.py', 'showmigrations', '--plan', capture=True, files=(rollback_override,))
            if snapshot_complete and restored_schema != prior_schema:
                raise RuntimeError('rollback restored services but not the prior database schema')
            setup_powersync_storage()
            compose('up', '-d', '--no-build', '--no-deps', '--force-recreate', 'powersync', files=(rollback_override,))
            resumed = compose('ps', '-q', 'powersync', capture=True, files=(rollback_override,))
            resumed_image = run('docker', '-H', docker_host, 'inspect', '-f', '{{.Image}}', resumed, capture=True)
            resumed_state = run('docker', '-H', docker_host, 'inspect', '-f', '{{.State.Status}}', resumed, capture=True)
            if resumed_image != prior_powersync_image or resumed_state != prior_powersync_state:
                raise RuntimeError('rollback restored the app but not exact PowerSync image/state')
            compose('exec', '-T', 'nginx', 'nginx', '-t')
            compose('exec', '-T', 'nginx', 'nginx', '-s', 'reload')
        except Exception as rollback_error:
            try:
                verify_binds(rendered_config(deploy_dir / 'compose.yaml'))
                compose('up', '-d', '--no-build', '--no-deps', '--force-recreate', *services)
                wait_healthy(compose('ps', '-q', 'web', capture=True))
                try:
                    setup_powersync_storage()
                    compose('up', '-d', '--no-build', '--no-deps', '--force-recreate', 'powersync')
                    for service, image in rollback_images.items():
                        container = compose('ps', '-q', service, capture=True)
                        actual_image = run('docker', '-H', docker_host, 'inspect', '-f', '{{.Image}}', container, capture=True)
                        actual_state = run('docker', '-H', docker_host, 'inspect', '-f', '{{.State.Status}}', container, capture=True)
                        if actual_image != image or actual_state != 'running':
                            raise RuntimeError('final restore did not recover prior image/running state: ' + service)
                finally:
                    compose('exec', '-T', 'nginx', 'nginx', '-t')
                    compose('exec', '-T', 'nginx', 'nginx', '-s', 'reload')
                with urlopen(public_url + '/en-au/user/login', timeout=30) as response:
                    if response.status != 200:raise RuntimeError('fallback public login did not return 200')
                fallback = 'previous compose running; public login 200; database recovery unproven'
            except Exception as fallback_error:
                fallback = f'final restore failed: {fallback_error}'
            raise SystemExit(f'release failed: {release_error}; rollback failed: {rollback_error}; {fallback}') from None
        raise SystemExit(f'web release failed: {release_error}; prior writer states, overrides, database schema/data and exact images restored') from None
print(json.dumps({'status': 'deployed', 'adapter': 'wger', 'commit': os.environ.get('WGER_RELEASE_COMMIT'), 'live_proof': live_proof}))
