import copy
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wger_preflight as gate
from deploy_wger import failure_reason


COMMIT = '0812ca39a80e82c071c99a54300df73e28668776'
CANDIDATE = 'sha256:f3bfc71c7693fef7baaccaf097d3a2ca2ba9076ce547b90d777e5b822f74c008'
BACKUP = 'sha256:' + '1' * 64
DIGEST = 'a' * 64


def protected(sessions, logs, **changes):
    return {'manager_workoutsession': {'rows': sessions, 'columns': {'id': DIGEST, 'date': DIGEST, **changes.get('session', {})}},
            'manager_workoutlog': {'rows': logs, 'columns': {'id': DIGEST, 'weight': DIGEST, **changes.get('log', {})}}}


def candidate_receipt(**changes):
    return {'repository': 'https://github.com/Cub-HQ/wger-backend', 'commit': COMMIT, 'archive_sha256': 'b' * 64,
            'dockerfile': 'extras/docker/production/Dockerfile', 'base_image': 'wger/base@sha256:' + 'c' * 64,
            'image_id': CANDIDATE, 'tag': 'fitness-wger-backend:' + COMMIT, 'app_build_commit': COMMIT, **changes}


def image_record(image=CANDIDATE, revision=COMMIT, build=COMMIT):
    return {'Id': image, 'Config': {'Labels': {'org.opencontainers.image.revision': revision},
                                    'Env': ['PATH=/usr/bin', 'APP_BUILD_COMMIT=' + build]}}


class PreflightTest(unittest.TestCase):
    def exercise(self, fault=None):
        source = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            destination = home / 'fitness-coach-migration'
            destination.mkdir(mode=0o700)
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(home / 'selected-docker.sock'))
            self.addCleanup(sock.close)
            deploy = home / 'fitness-wger'
            deploy.mkdir()
            for name in ('writer', 'history'):
                (home / name).touch()
            env = {'WGER_WRITER_LOCK': str(home / 'writer'), 'WGER_HISTORY_LOCK': str(home / 'history')}
            env['WGER_DOCKER_HOST'] = 'unix://' + str(home / 'selected-docker.sock')
            counts = {'users': 5, 'sessions': 387, 'logs': 4564, 'videos': 0, 'recoveries': 2}
            before = {'database': {'counts': counts, 'schema': [['manager', '0029']], 'protected': protected(387, 4564)},
                      'media': (b'', 0), 'containers': {name: {'Image': name, 'state': ('running', 'healthy')}
                       for name in ('web', 'powersync', 'celery_worker', 'celery_beat')}}
            before['files'] = {'overrides/react-main.js': ('bundle-digest', 0o644)}
            after = copy.deepcopy(before)
            if fault == 'live':
                after['containers']['web']['Image'] = 'changed-image'
            if fault == 'bundle':
                after['files']['overrides/react-main.js'] = ('changed-bundle', 0o644)
            if fault == 'migration':
                after['database']['schema'].append(['manager', '0030'])
            if fault == 'reload':
                after['database']['counts']['logs'] += 1
                after['media'] = (b'new athlete media', 1)
                after['containers']['web'].update(Id='recreated-id', RestartCount=1, HostConfig={'transient': 'changed'})
                after['files']['overrides/react-main.js'] = ('bundle-digest', 0o600)
                after['files']['overrides/transient.next'] = ('temporary', 0o600)
            restored = copy.deepcopy(before['database'])
            if fault == 'counts':
                restored['counts'] = {**counts, 'logs': 0}
            if fault == 'recoveries':
                restored['counts']['recoveries'] -= 1
            if fault == 'schema':
                restored['schema'] = [['manager', '0028']]
            if fault == 'protected-value':
                # Same counts, one edited set weight: counts alone would pass.
                restored['protected'] = protected(387, 4564, log={'weight': 'e' * 64})
            if fault == 'protected-column':
                restored['protected']['manager_workoutsession']['columns'].pop('date')
            if fault == 'added-column':
                restored['protected']['manager_workoutlog']['columns']['cardio'] = 'f' * 64
            staged = home / 'wger-product-candidate'
            (staged / 'overrides').mkdir(parents=True)
            (staged / 'compose.yaml').write_text('services: {}')
            (staged / 'overrides/pdf.py.next').write_text('candidate pdf')
            receipt_file = staged / 'overrides/backend-image.json.next'
            receipt_file.write_text(json.dumps(candidate_receipt()))
            project = 'wger-restore-1234567890'
            calls = []
            residue = False

            def command(args, environment):
                nonlocal residue
                args = [str(arg) for arg in args]
                calls.append(args)
                self.assertEqual(environment['WGER_DOCKER_HOST'], env['WGER_DOCKER_HOST'])
                if args[0] == 'docker':
                    self.assertEqual(args[:3], ['docker', '-H', env['WGER_DOCKER_HOST']])
                if 'info' in args:
                    return b'' if fault == 'daemon' else b'selected-daemon'
                if args[3:5] == ['image', 'inspect']:
                    self.assertEqual(args[5:], [CANDIDATE, 'fitness-wger-backend:' + COMMIT])
                    if fault == 'moved-tag':
                        return json.dumps([image_record(), image_record(BACKUP)]).encode()
                    return json.dumps([image_record(), image_record()]).encode()
                script = Path(args[1]).name if len(args) > 1 else ''
                if script == 'backup.py':
                    snapshot = destination / 'wger-20260925T000000000000Z'
                    snapshot.mkdir()
                    files = {'database.dump': b'database', 'media.tar': b'media', 'deployment.tar': b'deploy',
                             'media-sha256.txt': b'', 'images.json': json.dumps({name: {'image': name, 'status': 'running', 'health': 'healthy'} for name in before['containers']}).encode()}
                    for name, data in files.items():
                        (snapshot / name).write_bytes(data)
                    manifest = {'format': 2, 'includes_powersync_storage': True, 'powersync_storage_source': 'database.dump',
                                'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
                    (snapshot / 'manifest.json').write_text(json.dumps(manifest))
                    if fault == 'checksum':
                        (snapshot / 'database.dump').write_bytes(b'corrupt')
                    return json.dumps({'snapshot': str(snapshot)}).encode()
                if script == 'restore-drill.py':
                    self.assertEqual(args[3:], ['--candidate-image', CANDIDATE, '--candidate-deploy', str(staged.resolve())])
                    work = destination / project
                    work.mkdir()
                    image_source = 'snapshot' if fault == 'snapshot-image' else 'candidate'
                    receipt = {'snapshot': args[2], 'project': project, 'port': 18197,
                               'created_containers': [project + '-' + n for n in ('db', 'web', 'nginx')],
                               'volumes': [project + '-' + n for n in ('db', 'media', 'static')],
                               'networks': [project, project + '-front'],
                               'image_source': image_source, 'web_image': BACKUP if image_source == 'snapshot' else CANDIDATE,
                               'snapshot_web_image': BACKUP, 'candidate_deploy': str(staged.resolve()),
                               'candidate_web_mounts': [{'source': 'overrides/pdf.py', 'candidate_file': 'overrides/pdf.py.next',
                                                         'target': '/home/wger/src/wger/utils/pdf.py',
                                                         'sha256': hashlib.sha256(b'snapshot pdf' if fault == 'snapshot-binds'
                                                                                  else b'candidate pdf').hexdigest()}],
                               'state': 'restored-awaiting-independent-application-check'}
                    if fault == 'no-binds':
                        receipt.pop('candidate_web_mounts')
                    (work / 'receipt.json').write_text(json.dumps(receipt))
                    residue = True
                    if fault == 'restore':
                        raise RuntimeError('restore failed after ownership receipt')
                    return json.dumps({'receipt': str(work / 'receipt.json')}).encode()
                if script == 'cleanup-drill.py':
                    if fault == 'cleanup':
                        raise RuntimeError('cleanup failed')
                    receipt = Path(args[2])
                    data = json.loads(receipt.read_text())
                    data['state'] = 'drill resources cleaned; original snapshots retained'
                    receipt.write_text(json.dumps(data))
                    residue = fault == 'residue'
                    return b''
                if 'migrate' in args:
                    if fault == 'pending-migration':
                        raise RuntimeError('restored application has pending migrations')
                    return b''
                raise AssertionError(args)

            def resources(*args):
                self.assertEqual(args[0], ['docker', '-H', env['WGER_DOCKER_HOST']])
                return {'container': [project] if residue else [], 'volume': [], 'network': []}

            with patch.object(gate.Path, 'home', return_value=home), \
                    patch.object(gate, '_command', side_effect=command), \
                    patch.object(gate, '_baseline', side_effect=[before, after]) as baseline, \
                    patch.object(gate, '_resources', side_effect=resources), \
                    patch.object(gate, '_database', return_value=restored), \
                    patch.object(gate, '_media', return_value=(b'wrong', 1) if fault == 'media' else (b'', 0)), \
                    patch.object(gate, '_application', side_effect=RuntimeError('HTTP failure') if fault == 'http' else None,
                                 return_value={'version': '2.7'}):
                if fault and fault not in ('reload', 'added-column'):
                    with self.assertRaises((RuntimeError, ValueError)) as caught:
                        gate.run(source, deploy, env, candidate_receipt=receipt_file, candidate_commit=COMMIT)
                    if fault == 'daemon':
                        self.assertIn('Docker daemon', str(caught.exception))
                    if fault == 'recoveries':
                        self.assertEqual(str(caught.exception), 'restored counts or applied migration proof mismatch')
                    if fault in ('protected-value', 'protected-column'):
                        table = 'manager_workoutlog' if fault == 'protected-value' else 'manager_workoutsession'
                        self.assertEqual(str(caught.exception), 'candidate restore changed protected ' + table + ' rows')
                    if fault == 'snapshot-image':
                        self.assertEqual(str(caught.exception), 'restore did not run the candidate image')
                    if fault == 'moved-tag':
                        self.assertEqual(str(caught.exception), 'candidate image identity does not match receipt')
                    if fault in ('snapshot-binds', 'no-binds'):
                        self.assertEqual(str(caught.exception), 'restore did not run the candidate compose binds')
                    category = {'bundle': 'bundle_sha256', 'migration': 'migrations', 'live': 'images'}.get(fault)
                    if category:
                        self.assertEqual(str(caught.exception), 'live release identity changed during backup/restore preflight: ' + category)
                else:
                    result = gate.run(source, deploy, env, candidate_receipt=receipt_file, candidate_commit=COMMIT)
                    self.assertTrue(result['live_baseline_unchanged'])
                    self.assertTrue(result['disposable_resources_removed'])
                    self.assertTrue(result['protected_rows_unchanged'])
                    self.assertEqual(result['counts'], counts)
                    self.assertEqual(result['candidate'], {'commit': COMMIT, 'image_id': CANDIDATE, 'tag': 'fitness-wger-backend:' + COMMIT,
                                                           'deploy': str(staged.resolve())})
                    self.assertEqual([mount['sha256'] for mount in result['candidate_web_mounts']],
                                     [hashlib.sha256(b'candidate pdf').hexdigest()])
                    self.assertEqual({record['image'] for record in result['rollback_images'].values()},
                                     {'web', 'powersync', 'celery_worker', 'celery_beat'})
                self.assertEqual(baseline.call_count, 0 if fault in ('daemon', 'moved-tag') else 2)
            scripts = [Path(args[1]).name for args in calls]
            if fault not in ('checksum', 'daemon', 'moved-tag'):
                self.assertEqual(scripts.count('cleanup-drill.py'), 1)
            else:
                self.assertNotIn('restore-drill.py', scripts)
            if fault == 'moved-tag':
                self.assertNotIn('backup.py', scripts)

    def test_success_and_fail_closed_proof_matrix(self):
        for fault in (None, 'reload', 'bundle', 'migration', 'checksum', 'counts', 'recoveries', 'schema', 'pending-migration',
                      'media', 'http', 'restore', 'live', 'residue', 'cleanup', 'daemon', 'protected-value',
                      'protected-column', 'added-column', 'snapshot-image', 'moved-tag', 'snapshot-binds', 'no-binds'):
            with self.subTest(fault=fault):
                self.exercise(fault)

    def test_candidate_identity_is_required(self):
        with self.assertRaises(TypeError):
            gate.run(Path('.'), Path('.'), {})

    def test_invalid_candidate_receipt_or_image_refuses(self):
        good = [image_record(), image_record()]
        cases = {
            'stale commit': (candidate_receipt(commit='d' * 40), good, 'does not match candidate commit'),
            'app build': (candidate_receipt(app_build_commit='d' * 40), good, 'does not match candidate commit'),
            'foreign tag': (candidate_receipt(tag='wger/server:latest'), good, 'does not match candidate commit'),
            'mutable id': (candidate_receipt(image_id='fitness-wger-backend:' + COMMIT), good, 'does not match candidate commit'),
            'short id': (candidate_receipt(image_id='sha256:f3bfc71c'), good, 'does not match candidate commit'),
            'backup image': (candidate_receipt(image_id=BACKUP), good, 'identity does not match'),
            'revision label': (candidate_receipt(), [image_record(revision='d' * 40)] * 2, 'identity does not match'),
            'env commit': (candidate_receipt(), [image_record(build='d' * 40)] * 2, 'identity does not match'),
            'missing image': (candidate_receipt(), None, 'candidate image unavailable'),
            'not json': ('not json', good, 'unreadable'),
        }
        for name, (receipt, records, cause) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'overrides/backend-image.json.next'
                path.parent.mkdir()
                (path.parent.parent / 'compose.yaml').write_text('services: {}')
                path.write_text(receipt if isinstance(receipt, str) else json.dumps(receipt))

                def command(args, env):
                    if records is None:
                        raise gate.subprocess.CalledProcessError(1, args)
                    return json.dumps(records).encode()
                with patch.object(gate, '_command', side_effect=command):
                    with self.assertRaisesRegex(RuntimeError, cause):
                        gate._candidate(['docker'], {}, path, COMMIT)
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / 'overrides/backend-image.json.next'
            for commit in ('0812ca39', COMMIT.upper(), None):
                with self.subTest(commit=commit), self.assertRaisesRegex(RuntimeError, 'full lowercase git sha'):
                    gate._candidate(['docker'], {}, absent, commit)
            with self.assertRaisesRegex(RuntimeError, 'no compose.yaml'):
                gate._candidate(['docker'], {}, absent, COMMIT)
            (Path(tmp) / 'compose.yaml').write_text('services: {}')
            with self.assertRaisesRegex(RuntimeError, 'unreadable'):
                gate._candidate(['docker'], {}, absent, COMMIT)
            with self.assertRaisesRegex(RuntimeError, 'must be <candidate>/overrides'):
                gate._candidate(['docker'], {}, Path(tmp) / 'backend-image.json.next', COMMIT)

    def test_invalid_endpoint_refuses_before_backup(self):
        for endpoint, cause in (('', 'Docker endpoint'), ('tcp://localhost:2375', 'Docker endpoint'),
                                ('unix://relative.sock', 'Docker endpoint'),
                                ('unix:///absent-wger-test.sock', 'Docker socket')):
            with self.subTest(endpoint=endpoint), patch.object(gate, '_command') as command:
                with self.assertRaisesRegex(RuntimeError, cause):
                    gate.run(Path('.'), Path.home() / 'fitness-wger', {'WGER_DOCKER_HOST': endpoint},
                             candidate_receipt=Path('absent'), candidate_commit=COMMIT)
                command.assert_not_called()

    def test_real_baseline_identity_ignores_restart_but_detects_bundle_bytes(self):
        services = ('web', 'celery_worker', 'celery_beat', 'powersync', 'db', 'cache', 'nginx')
        records = [{'Id': name, 'Image': 'sha256:' + name,
                    'Config': {'Labels': {'com.docker.compose.service': name}},
                    'HostConfig': {}, 'Mounts': [], 'RestartCount': 0,
                    'State': {'Status': 'running', 'Health': {'Status': 'healthy'}}} for name in services]
        database = {'counts': {'users': 1, 'sessions': 2, 'logs': 3, 'videos': 0, 'recoveries': 0}, 'schema': [['manager', '0029']]}
        def command(args, env):
            return json.dumps(records).encode() if 'inspect' in args else b'web worker beat powersync db cache nginx'
        with tempfile.TemporaryDirectory() as temporary:
            deploy = Path(temporary)
            (deploy / 'config').mkdir(); (deploy / 'overrides').mkdir()
            (deploy / 'config/private.env').write_text('private fixture')
            (deploy / 'compose.yaml').write_text('fixture')
            bundle = deploy / 'overrides/react-main.js'; bundle.write_bytes(b'original bundle')
            with patch.object(gate, '_command', side_effect=command), patch.object(gate, '_database', side_effect=lambda *args: copy.deepcopy(database)), patch.object(gate, '_media', return_value=(b'', 0)):
                before = gate._release_identity(gate._baseline(['docker'], deploy, {}))
                records[0]['RestartCount'] += 1
                records[0]['State']['StartedAt'] = 'later'
                records[0]['NetworkSettings'] = {'IPAddress': 'new'}
                database['counts']['logs'] += 1
                (deploy / 'overrides/transient.next').write_text('temporary')
                self.assertEqual(gate._release_identity(gate._baseline(['docker'], deploy, {})), before)
                bundle.write_bytes(b'changed bundle')
                self.assertNotEqual(gate._release_identity(gate._baseline(['docker'], deploy, {})), before)
                records[0]['State']['Health']['Status'] = 'unhealthy'
                with self.assertRaisesRegex(RuntimeError, 'not healthy'):
                    gate._baseline(['docker'], deploy, {})

    def test_liveness_refusal_names_checked_target_and_failed_service(self):
        services = ('web', 'celery_worker', 'celery_beat', 'powersync', 'db', 'cache', 'nginx')
        records = [{'Id': name, 'Image': 'sha256:' + name,
                    'Config': {'Env': ['PASSWORD=never-print-private-env'],
                               'Labels': {'com.docker.compose.service': name,
                                          'com.docker.compose.project': 'fitness-wger'}},
                    'HostConfig': {}, 'Mounts': [], 'RestartCount': 0,
                    'State': {'Status': 'running', 'Health': {'Status': 'healthy'}, 'ExitCode': 0}}
                   for name in services]
        for state in ('restarting', 'exited'):
            with self.subTest(state=state):
                records[3]['State'].update(Status=state, ExitCode=150)
                records[3]['RestartCount'] = 140
                with patch.object(gate, '_command', side_effect=[b'container-ids', json.dumps(records).encode()]), \
                        patch.object(gate, '_database') as database, patch.object(gate, '_media') as media:
                    with self.assertRaises(RuntimeError) as caught:
                        gate._baseline(['docker', '-H', 'unix:///reviewed/docker.sock'], Path('/live/fitness-wger'), {})
                    message = failure_reason(caught.exception)
                    for expected in ('live gym is not running', '/reviewed/docker.sock', '/live/fitness-wger/compose.yaml',
                                     'fitness-wger', 'powersync', state, '150', '140'):
                        self.assertIn(expected, message)
                    self.assertNotIn('never-print-private-env', message)
                    database.assert_not_called()
                    media.assert_not_called()

    def test_media_uses_raw_inventory_and_independent_count(self):
        with patch.object(gate, '_command', side_effect=[b'hash  ./a\n', b'1\n']):
            self.assertEqual(gate._media(['docker'], 'owned-media', {}), (b'hash  ./a\n', 1))

    def test_application_accepts_real_string_and_legacy_object(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(payload).encode()
        for payload in ('2.7.0', {'version': '2.7.0'}):
            with self.subTest(payload=payload), patch.object(gate.urllib.request, 'urlopen', return_value=Response()), patch.object(gate.time, 'sleep'):
                self.assertEqual(gate._application(18197), {'version': '2.7.0'})

    def test_application_rejects_empty_http_success(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{}'
        with patch.object(gate.urllib.request, 'urlopen', return_value=Response()), patch.object(gate.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'version endpoint'):
                gate._application(18197)


if __name__ == '__main__':
    unittest.main()
