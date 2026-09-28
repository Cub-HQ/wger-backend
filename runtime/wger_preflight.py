"""Canonical issue-83 backup/restore gate; never stages or releases live files."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request


PROTECTED = ('manager_workoutsession', 'manager_workoutlog')
CANDIDATE_REPOSITORY = 'fitness-wger-backend:'
IMAGE_ID = re.compile(r'sha256:[0-9a-f]{64}')

DATABASE_PROOF = """import hashlib, json
from django.apps import apps
from django.db.migrations.recorder import MigrationRecorder
from django.db import connection
schema = sorted([list(row) for row in MigrationRecorder.Migration.objects.values_list('app','name')])
counts = {name: apps.get_model(app, model).objects.count() for name, app, model in [('users','auth','User'),('sessions','manager','WorkoutSession'),('logs','manager','WorkoutLog'),('videos','exercises','ExerciseVideo')]}
recovery_table = 'manager_workoutsessionrecovery'
if recovery_table in connection.introspection.table_names():
    with connection.cursor() as cursor:
        cursor.execute('SELECT COUNT(*) FROM ' + connection.ops.quote_name(recovery_table))
        counts['recoveries'] = cursor.fetchone()[0]
elif ['manager', '0031_workoutsessionrecovery'] in schema:
    raise RuntimeError('applied recovery migration is missing its table')
else:
    # Preflight runs against the live baseline before the first recovery release.
    counts['recoveries'] = 0
# Per-column digests keyed by primary key: equal counts cannot hide an edited session or set.
protected = {}
for table in ('manager_workoutsession', 'manager_workoutlog'):
    with connection.cursor() as cursor:
        columns = sorted(column.name for column in connection.introspection.get_table_description(cursor, table))
        if 'id' not in columns:
            raise RuntimeError('protected table has no id column: ' + table)
        digests = {column: hashlib.sha256() for column in columns}
        quoted = ', '.join(connection.ops.quote_name(column) for column in columns)
        cursor.execute('SELECT ' + quoted + ' FROM ' + connection.ops.quote_name(table) + ' ORDER BY ' + connection.ops.quote_name('id'))
        rows = 0
        for row in cursor.fetchall():
            rows += 1
            key = row[columns.index('id')]
            for column, value in zip(columns, row):
                digests[column].update(json.dumps([key, value], default=str).encode() + b'\\n')
    protected[table] = {'rows': rows, 'columns': {column: digest.hexdigest() for column, digest in digests.items()}}
print(json.dumps({'counts': counts, 'schema': schema, 'protected': protected}))
"""


def _command(args, env):
    return subprocess.run([str(arg) for arg in args], env=env, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def _database(docker, container, env):
    output = _command([*docker, 'exec', container, 'python3', 'manage.py', 'shell', '-c', DATABASE_PROOF], env)
    proof = json.loads(output.splitlines()[-1])
    if (set(proof['counts']) != {'users', 'sessions', 'logs', 'videos', 'recoveries'}
            or any(type(n) is not int or n < 0 for n in proof['counts'].values())
            or not proof['schema']):
        raise RuntimeError('incomplete database proof')
    protected = proof.get('protected')
    if (not isinstance(protected, dict) or set(protected) != set(PROTECTED)
            or protected['manager_workoutsession'].get('rows') != proof['counts']['sessions']
            or protected['manager_workoutlog'].get('rows') != proof['counts']['logs']
            or any(not isinstance(protected[table].get('columns'), dict) or 'id' not in protected[table]['columns']
                   or not all(isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest)
                              for digest in protected[table]['columns'].values())
                   for table in PROTECTED)):
        raise RuntimeError('incomplete protected session/set proof')
    return proof


def _protected_unchanged(before, after):
    """Candidate migrations may add columns; any existing session/set value or row change refuses."""
    for table in PROTECTED:
        old, new = before['protected'][table], after['protected'][table]
        if old['rows'] != new['rows'] or any(new['columns'].get(column) != digest
                                             for column, digest in old['columns'].items()):
            raise RuntimeError('candidate restore changed protected ' + table + ' rows')


def _candidate(docker, env, receipt_path, commit):
    """Return the immutable candidate image named by the prepared receipt, checked against Docker."""
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise RuntimeError('candidate commit must be a full lowercase git sha')
    receipt_path = Path(receipt_path)
    if receipt_path.name != 'backend-image.json.next' or receipt_path.parent.name != 'overrides':
        raise RuntimeError('candidate receipt must be <candidate>/overrides/backend-image.json.next')
    deploy = receipt_path.resolve().parent.parent
    if not (deploy / 'compose.yaml').is_file():
        raise RuntimeError('candidate deployment has no compose.yaml')
    try:
        receipt = json.loads(Path(receipt_path).read_text())
    except (OSError, TypeError, ValueError) as error:
        raise RuntimeError('candidate image receipt unreadable') from error
    if (not isinstance(receipt, dict) or receipt.get('commit') != commit
            or receipt.get('app_build_commit') != commit
            or receipt.get('tag') != CANDIDATE_REPOSITORY + commit
            or not isinstance(receipt.get('image_id'), str) or not IMAGE_ID.fullmatch(receipt['image_id'])):
        raise RuntimeError('candidate image receipt does not match candidate commit')
    image = receipt['image_id']
    try:
        records = json.loads(_command([*docker, 'image', 'inspect', image, receipt['tag']], env))
    except (subprocess.CalledProcessError, ValueError) as error:
        raise RuntimeError('candidate image unavailable: ' + image) from error
    # Inspecting the tag too catches a stale receipt whose tag was since moved to another image.
    if len(records) != 2 or any(
            record.get('Id') != image
            or (record.get('Config') or {}).get('Labels', {}).get('org.opencontainers.image.revision') != commit
            or 'APP_BUILD_COMMIT=' + commit not in ((record.get('Config') or {}).get('Env') or [])
            for record in records):
        raise RuntimeError('candidate image identity does not match receipt')
    return {'commit': commit, 'image_id': image, 'tag': receipt['tag'], 'deploy': str(deploy)}


def _media(docker, volume, env):
    prefix = [*docker, 'run', '--rm', '--network', 'none', '-v', volume + ':/media:ro', 'alpine:3.22', 'sh', '-c']
    raw = _command([*prefix, 'cd /media && find . -type f -exec sha256sum {} + | sort'], env)
    count = int(_command([*prefix, 'find /media -type f | wc -l'], env))
    return raw, count


def _resources(docker, env, project=None):
    label = 'fitness.backup.restore-drill' + ('=' + project if project else '')
    return {kind: sorted(_command([*docker, kind, 'ls', '-q', *(['-a'] if kind == 'container' else []), '--filter', 'label=' + label], env).decode().split())
            for kind in ('container', 'volume', 'network')}


def _baseline(docker, deploy, env):
    host = docker[docker.index('-H') + 1].removeprefix('unix://') if '-H' in docker else 'default'
    target = f'socket={host} compose={deploy / "compose.yaml"}'
    ids = _command([*docker, 'compose', '-f', deploy / 'compose.yaml', 'ps', '-a', '-q'], env).decode().split()
    if not ids:
        raise RuntimeError('live gym containers unavailable: ' + target)
    records = json.loads(_command([*docker, 'inspect', *ids], env))
    states = {obj['Config']['Labels'].get('com.docker.compose.service'): {
        'project': obj['Config']['Labels'].get('com.docker.compose.project'),
        'status': obj['State']['Status'], 'health': obj['State'].get('Health', {}).get('Status'),
        'exit': obj['State'].get('ExitCode'), 'restarts': obj['RestartCount'],
    } for obj in records}
    print('wger liveness: ' + target + ' services=' + json.dumps(states, sort_keys=True), file=sys.stderr)
    containers = {}
    web = None
    for obj in records:
        service = obj['Config']['Labels'].get('com.docker.compose.service')
        state = obj['State']
        containers[service] = {key: obj[key] for key in ('Id', 'Image', 'Config', 'HostConfig', 'Mounts', 'RestartCount')}
        containers[service]['state'] = (state['Status'], state.get('Health', {}).get('Status'))
        if service == 'web':
            web = obj['Id']
    if not {'web', 'celery_worker', 'celery_beat', 'powersync', 'db', 'cache', 'nginx'} <= containers.keys():
        raise RuntimeError('incomplete live container baseline: ' + target + ' services=' + json.dumps(states, sort_keys=True))
    stopped = {name: states[name] for name in containers if containers[name]['state'][0] != 'running'}
    if stopped:
        raise RuntimeError('live gym is not running: ' + target + ' services=' + json.dumps(stopped, sort_keys=True))
    if containers['web']['state'][1] != 'healthy':
        raise RuntimeError('live web is not healthy: ' + target + ' web=' + json.dumps(states['web'], sort_keys=True))
    files = {}
    for folder in ('config', 'overrides'):
        for path in sorted((deploy / folder).rglob('*')):
            if path.is_file():
                files[str(path.relative_to(deploy))] = (hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(path.stat().st_mode))
    files['compose.yaml'] = hashlib.sha256((deploy / 'compose.yaml').read_bytes()).hexdigest()
    if 'config/private.env' not in files:
        raise RuntimeError('private environment missing')
    return {'containers': containers, 'files': files, 'database': _database(docker, web, env),
            'media': _media(docker, 'fitness-wger_media', env)}


def _release_identity(baseline):
    """Deployment identity excludes restart metadata and ongoing athlete writes."""
    return {
        'bundle_sha256': baseline['files']['overrides/react-main.js'][0],
        'migrations': sorted(baseline['database']['schema']),
        'images': {service: record['Image'] for service, record in baseline['containers'].items()},
    }


def _application(port):
    url = f'http://127.0.0.1:{port}/api/v2/version/'
    for attempt in range(60):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                proof = json.load(response)
                if isinstance(proof, str):
                    proof = {'version': proof.strip()}
                if response.status == 200 and isinstance(proof, dict) and isinstance(proof.get('version'), str) and proof['version'].strip():
                    return proof
        except (OSError, ValueError, urllib.error.URLError):
            pass
        if attempt < 59:
            time.sleep(1)
    raise RuntimeError('restored application version endpoint did not become ready')


def _receipt(path, snapshot):
    receipt = json.loads(path.read_text())
    project = receipt['project']
    if (not re.fullmatch(r'wger-restore-[0-9a-f]{10}', project)
            or path.parent.name != project or path.parent.parent != snapshot.parent
            or Path(receipt['snapshot']) != snapshot
            or set(receipt['created_containers']) != {project + '-' + n for n in ('db', 'web', 'nginx')}
            or set(receipt['volumes']) != {project + '-' + n for n in ('db', 'media', 'static')}
            or set(receipt['networks']) != {project, project + '-front'}):
        raise RuntimeError('restore receipt ownership mismatch')
    return receipt


def run(source: Path, deploy: Path, env: dict, *, candidate_receipt: Path, candidate_commit: str) -> dict:
    """Return evidence only after the candidate image restored proof, owned cleanup and live parity.

    The snapshot's own images stay the rollback identity; the drill runs the prepared candidate.
    """
    source, deploy = Path(source), Path(deploy)
    env = {**os.environ, **env}
    home = Path.home()
    requested = env.get('WGER_DOCKER_HOST', f'unix://{home}/.colima/default/docker.sock')
    if deploy.resolve() != (home / 'fitness-wger').resolve():
        raise RuntimeError('preflight requires the reviewed local gym')
    if not requested.startswith('unix://') or not Path(requested[7:]).is_absolute():
        raise RuntimeError('Docker endpoint must be an absolute Unix socket')
    if not Path(requested[7:]).is_socket():
        raise RuntimeError('Docker socket is unavailable: ' + requested)
    env['WGER_DOCKER_HOST'] = requested
    docker = ['docker', '-H', requested]
    try:
        identity = _command([*docker, 'info', '--format', '{{.ID}}'], env).strip()
    except subprocess.CalledProcessError as error:
        raise RuntimeError('Docker daemon is unavailable: ' + requested) from error
    if not identity:
        raise RuntimeError('Docker daemon identity is unavailable: ' + requested)
    if not all(env.get(key) and Path(env[key]).is_file() for key in ('WGER_WRITER_LOCK', 'WGER_HISTORY_LOCK')):
        raise RuntimeError('reviewed writer and history locks required')
    destination = home / 'fitness-coach-migration'
    state = destination.lstat()
    if not stat.S_ISDIR(state.st_mode) or state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) != 0o700:
        raise RuntimeError('established private backup destination required')
    operations = source / 'deployment/wger/operations'
    spec = importlib.util.spec_from_file_location('wger_snapshot_preflight', operations / 'snapshot.py')
    snapshot_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(snapshot_module)
    candidate = _candidate(docker, env, candidate_receipt, candidate_commit)
    before = _baseline(docker, deploy, env)
    residues = _resources(docker, env)
    previous = set(destination.glob('wger-restore-*'))
    snapshot = None
    receipt_path = None
    evidence = {}
    try:
        output = _command([sys.executable, operations / 'backup.py', '--destination', destination], env)
        snapshot = Path(json.loads(output.splitlines()[-1])['snapshot'])
        if snapshot.parent != destination or not re.fullmatch(r'wger-[0-9TZ]+', snapshot.name):
            raise RuntimeError('backup returned an unexpected snapshot path')
        manifest = snapshot_module.verify_snapshot(snapshot)
        if not {'media-sha256.txt', 'images.json'} <= manifest['files'].keys():
            raise RuntimeError('backup omits media or writer identity proof')
        raw = (snapshot / 'media-sha256.txt').read_bytes()
        if raw != before['media'][0]:
            raise RuntimeError('snapshot media differs from live baseline')
        if json.loads((snapshot / 'images.json').read_text()) != {
                service: {'image': before['containers'][service]['Image'],
                          'status': before['containers'][service]['state'][0],
                          'health': before['containers'][service]['state'][1]}
                for service in ('powersync', 'web', 'celery_worker', 'celery_beat')}:
            raise RuntimeError('snapshot writer identities differ from baseline')
        output = _command([sys.executable, operations / 'restore-drill.py', snapshot,
                           '--candidate-image', candidate['image_id'], '--candidate-deploy', candidate['deploy']], env)
        receipt_path = Path(json.loads(output.splitlines()[-1])['receipt'])
        receipt = _receipt(receipt_path, snapshot)
        if receipt.get('state') != 'restored-awaiting-independent-application-check':
            raise RuntimeError('restore is incomplete')
        if receipt.get('image_source') != 'candidate' or receipt.get('web_image') != candidate['image_id']:
            raise RuntimeError('restore did not run the candidate image')
        mounts = receipt.get('candidate_web_mounts')
        if (receipt.get('candidate_deploy') != candidate['deploy'] or not isinstance(mounts, list) or not mounts
                or any(not isinstance(mount, dict) or mount.get('sha256') != hashlib.sha256(
                    (Path(candidate['deploy']) / mount.get('candidate_file', '')).read_bytes()).hexdigest()
                    for mount in mounts)):
            raise RuntimeError('restore did not run the candidate compose binds')
        application = _application(receipt['port'])
        restored = _database(docker, receipt['project'] + '-web', env)
        schema = {tuple(row) for row in restored['schema']}
        if (restored['counts'] != before['database']['counts']
                or not {tuple(row) for row in before['database']['schema']} <= schema):
            raise RuntimeError('restored counts or applied migration proof mismatch')
        _protected_unchanged(before['database'], restored)
        _command([*docker, 'exec', receipt['project'] + '-web', 'python3', 'manage.py', 'migrate', '--check'], env)
        if _media(docker, receipt['project'] + '-media', env) != before['media']:
            raise RuntimeError('restored canonical media inventory/count mismatch')
        evidence = {'snapshot': str(snapshot), 'receipt': str(receipt_path), 'format': manifest['format'],
                    'application': application, 'counts': restored['counts'], 'schema': restored['schema'],
                    'media_sha256': hashlib.sha256(raw).hexdigest(), 'media_files': before['media'][1],
                    'candidate': candidate, 'rollback_images': json.loads((snapshot / 'images.json').read_text()),
                    'candidate_web_mounts': mounts, 'protected_rows_unchanged': True}
    finally:
        failures = []
        # A failed restore may have written its ownership receipt before exiting.
        for work in sorted(set(destination.glob('wger-restore-*')) - previous):
            path = work / 'receipt.json'
            try:
                if not path.is_file() or snapshot is None:
                    raise RuntimeError('restore work directory has no owned cleanup receipt')
                _receipt(path, snapshot)
                _command([sys.executable, operations / 'cleanup-drill.py', path], env)
            except Exception:
                failures.append('receipt-owned restore cleanup failed')
        try:
            if _resources(docker, env) != residues:
                failures.append('disposable restore resources remain or baseline resources changed')
        except Exception:
            failures.append('disposable residue accounting unavailable')
        try:
            before_identity = _release_identity(before)
            after_identity = _release_identity(_baseline(docker, deploy, env))
            changed = [key for key in before_identity if before_identity[key] != after_identity[key]]
            if changed:
                failures.append('live release identity changed during backup/restore preflight: ' + ', '.join(changed))
        except Exception:
            failures.append('live baseline comparison unavailable')
        if failures:
            raise RuntimeError('; '.join(failures))
    if receipt_path is None or json.loads(receipt_path.read_text()).get('state') != 'drill resources cleaned; original snapshots retained':
        raise RuntimeError('receipt-owned cleanup was not completed')
    return {**evidence, 'live_baseline_unchanged': True, 'disposable_resources_removed': True,
            'retained_private_artifacts': [str(snapshot), str(receipt_path.parent)]}
