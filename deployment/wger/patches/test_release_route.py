import hashlib
import fcntl
import os
import pathlib
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import http.server
import threading

ROOT = pathlib.Path(__file__).parent
RECOVERY_TARGETS = {
    'manager-log.py': 'wger/manager/models/log.py',
    'manager-0030-workoutlog-cardio-metrics.py': 'wger/manager/migrations/0030_workoutlog_cardio_metrics.py',
    'manager-session-recovery.py': 'wger/manager/models/session_recovery.py',
    'manager-models-init.py': 'wger/manager/models/__init__.py',
    'manager-api-views.py': 'wger/manager/api/views.py',
    'manager-tasks.py': 'wger/manager/tasks.py',
    'manager-0031-session-recovery.py': 'wger/manager/migrations/0031_workoutsessionrecovery.py',
}
ARTIFACT_NAMES = ('react-main.js', 'template.html', 'history-overview.html', 'api-key.html',
                  'pdf.py', 'corresponding-source.json', *RECOVERY_TARGETS)


class ReleaseRouteTest(unittest.TestCase):
    def test_compose_mounts_recovery_in_every_writer(self):
        try:
            import yaml
        except ImportError:
            self.skipTest('PyYAML unavailable; no dependency installed by release tests')
        services = yaml.safe_load((ROOT.parent / 'compose.yaml').read_text())['services']
        targets = {**RECOVERY_TARGETS, 'settings-main.py': 'settings/main.py'}
        for service in ('web', 'celery_worker', 'celery_beat'):
            for name, target in targets.items():
                with self.subTest(service=service, artifact=name):
                    mounts = [mount.split(':') for mount in services[service]['volumes']]
                    matching = [mount for mount in mounts if mount[1] == '/home/wger/src/' + target]
                    self.assertEqual(matching, [['./overrides/' + name, '/home/wger/src/' + target, 'ro']])

    def test_recovery_schedule_is_independent_of_email(self):
        import ast
        from datetime import timedelta
        from unittest.mock import Mock
        tree = ast.parse((ROOT.parent / 'overrides/settings-main.py').read_text())
        names = {'CELERY_IMPORTS', 'CELERY_BEAT_SCHEDULE'}
        statements = [node for node in tree.body if any(
            isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store) and child.id in names
            for child in ast.walk(node))]
        for enabled in (False, True):
            with self.subTest(email_enabled=enabled):
                namespace = {'timedelta': timedelta, 'WGER_SETTINGS': {'USE_CELERY': enabled},
                             'env': Mock(**{'bool.return_value': enabled}),
                             'CELERY_IMPORTS': ('existing.tasks',),
                             'CELERY_BEAT_SCHEDULE': {'existing-task': {'task': 'existing.task'}}}
                exec(compile(ast.Module(body=statements, type_ignores=[]), '<recovery-settings>', 'exec'), namespace)
                self.assertIn('wger.manager.tasks', namespace['CELERY_IMPORTS'])
                self.assertIn('existing.tasks', namespace['CELERY_IMPORTS'])
                self.assertEqual(namespace['CELERY_BEAT_SCHEDULE']['existing-task'], {'task': 'existing.task'})
                self.assertEqual(namespace['CELERY_BEAT_SCHEDULE']['purge-session-recoveries'],
                                 {'task': 'wger.manager.tasks.purge_session_recoveries', 'schedule': timedelta(hours=1)})

    def test_public_readiness_retries_only_transient_transport_failures(self):
        import ast
        from unittest.mock import Mock, patch
        from urllib.error import HTTPError, URLError
        import ssl
        tree = ast.parse((ROOT / 'release_web.py').read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'public_response')
        namespace = {'HTTPError': HTTPError, 'URLError': URLError, 'time': time}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<public_response>', 'exec'), namespace)
        response = object()
        for error in (HTTPError('url', 502, 'upstream', {}, None), HTTPError('url', 503, 'upstream', {}, None),
                      HTTPError('url', 504, 'upstream', {}, None), URLError(ConnectionRefusedError()),
                      ConnectionResetError(), TimeoutError()):
            with self.subTest(transient=type(error).__name__), patch.object(time, 'sleep'):
                opener = namespace['urlopen'] = Mock(side_effect=[error, response])
                self.assertIs(namespace['public_response']('url', timeout=30), response)
                self.assertEqual(opener.call_count, 2)
        for error in (HTTPError('url', 403, 'denied', {}, None), URLError(ssl.SSLCertVerificationError()),
                      OSError('unknown transport failure'), ValueError('invalid response')):
            with self.subTest(permanent=type(error).__name__), patch.object(time, 'sleep'):
                opener = namespace['urlopen'] = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    namespace['public_response']('url', timeout=30)
                self.assertEqual(opener.call_count, 1)
                if isinstance(error, HTTPError): error.close()
        with patch.object(time, 'sleep'):
            opener = namespace['urlopen'] = Mock(side_effect=HTTPError('url', 502, 'upstream', {}, None))
            with self.assertRaises(HTTPError):
                namespace['public_response']('url', timeout=30)
            self.assertEqual(opener.call_count, 10)
            opener.side_effect.close()
        with patch.object(time, 'sleep'), patch.object(time, 'monotonic', side_effect=[0, 0, 30]):
            error = HTTPError('url', 502, 'upstream', {}, None)
            opener = namespace['urlopen'] = Mock(side_effect=error)
            with self.assertRaises(HTTPError):
                namespace['public_response']('url', timeout=30)
            self.assertEqual(opener.call_count, 1)
            error.close()
        import io
        import re
        from urllib.parse import urljoin
        from urllib.request import Request
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'http_bundle')
        namespace.update({'public_url': 'https://gym.invalid', 'Request': Request, 'urljoin': urljoin, 're': re})
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<http_bundle>', 'exec'), namespace)
        login = io.BytesIO(b'<script src="/static/node/@wger-project/react-components/build/main.abc123.js"></script>')
        bundle = io.BytesIO(b'wrong bundle')
        bundle.headers = {'Date': 'today', 'Last-Modified': 'yesterday'}
        namespace['public_response'] = Mock(side_effect=[login, bundle])
        with self.assertRaisesRegex(OSError, 'does not match staged release'):
            namespace['http_bundle'](b'correct bundle')
        self.assertEqual(namespace['public_response'].call_count, 2)

    def test_preparation_extracts_the_compose_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            socket_path = root / '.colima/default/docker.sock'
            socket_path.parent.mkdir(parents=True)
            with socket.socket(socket.AF_UNIX) as docker_socket:
                docker_socket.bind(str(socket_path))
                patches = root / 'deployment/patches'
                patches.mkdir(parents=True)
                script = patches / 'prepare-react.sh'
                script.write_bytes((ROOT / 'prepare-react.sh').read_bytes())
                binary = root / 'bin/docker'
                binary.parent.mkdir()
                binary.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ['DOCKER_LOG']).open('a') as log: log.write(json.dumps(args) + '\\n')
if 'config' in args: print(json.dumps({'services': {'web': {'image': 'stock-fixture@sha256:abc'}}}))
elif 'create' in args: sys.exit(7)
''')
                binary.chmod(0o755)
                log = root / 'docker.jsonl'
                env = {**os.environ, 'HOME': str(root), 'WGER_DOCKER_HOST': 'unix://' + str(socket_path),
                       'PATH': str(binary.parent) + ':' + os.environ['PATH'], 'DOCKER_LOG': str(log)}
                result = subprocess.run(['bash', str(script)], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 7, result.stderr)
                commands = [json.loads(line) for line in log.read_text().splitlines()]
                create = next(command for command in commands if 'create' in command)
                self.assertEqual(create[-1], 'stock-fixture@sha256:abc')
                self.assertFalse(any('up' in command or 'build' in command for command in commands))

    def test_preparation_publishes_only_complete_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            socket_path = root / '.colima/default/docker.sock'
            socket_path.parent.mkdir(parents=True)
            patches = root / 'deployment/patches'
            patches.mkdir(parents=True)
            script = patches / 'prepare-react.sh'
            script.write_bytes((ROOT / 'prepare-react.sh').read_bytes())
            overrides = patches.parent / 'overrides'
            overrides.mkdir()
            for name in ARTIFACT_NAMES:
                (overrides / (name + '.next')).write_text('previous candidate ' + name)
                (overrides / name).write_text('live ' + name)
            binary = root / 'bin'
            binary.mkdir()
            fake = f'#!{sys.executable}\n' + '''import json, os, sys
from pathlib import Path
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
if tool == 'docker':
    if 'config' in args: print(json.dumps({'services': {'web': {'image': 'fixture-image'}}}))
    elif 'run' in args:
        overrides = Path(os.environ['PREPARE_OVERRIDES'])
        candidates = {path.name: path.read_text() for path in overrides.glob('*.next')}
        if candidates != json.loads(os.environ['PRIOR_CANDIDATES']):
            print('candidates published before ORM proof', file=sys.stderr)
            sys.exit(11)
        Path(os.environ['ORM_LOG']).write_text(json.dumps(args))
        if os.environ.get('FAIL_ORM') == '1':
            print('ORM recovery proof rejected', file=sys.stderr)
            sys.exit(10)
elif tool == 'python3':
    if args[0] == '-c': os.execv(os.environ['REAL_PYTHON'], [os.environ['REAL_PYTHON'], *args])
    generator = Path(args[0]).name
    if generator == 'patch_session_recovery.py':
        for name in json.loads(os.environ['RECOVERY_TARGETS']):
            (Path(args[-1]) / (name + '.next')).write_text('prepared ' + name)
        if os.environ.get('FAIL_RECOVERY') == '1':
            print('backend anchor rejected', file=sys.stderr)
            sys.exit(9)
    elif args[-1].endswith('.next'):
        Path(args[-1]).write_text('prepared ' + Path(args[-1]).name.removesuffix('.next'))
'''
            for name in ('docker', 'curl', 'tar', 'npm', 'python3'):
                command = binary / name
                command.write_text(fake)
                command.chmod(0o755)
            env = {**os.environ, 'HOME': str(root), 'PATH': str(binary) + ':' + os.environ['PATH'],
                   'WGER_DOCKER_HOST': 'unix://' + str(socket_path), 'REAL_PYTHON': sys.executable,
                   'PREPARE_OVERRIDES': str(overrides), 'ORM_LOG': str(root / 'orm.json'),
                   'PRIOR_CANDIDATES': json.dumps({name + '.next': 'previous candidate ' + name for name in ARTIFACT_NAMES}),
                   'RECOVERY_TARGETS': json.dumps(RECOVERY_TARGETS)}
            with socket.socket(socket.AF_UNIX) as docker_socket:
                docker_socket.bind(str(socket_path))
                failed = subprocess.run(['bash', str(script)], env={**env, 'FAIL_RECOVERY': '1'},
                                        text=True, capture_output=True)
                self.assertEqual(failed.returncode, 9, failed.stderr)
                self.assertIn('backend anchor rejected', failed.stderr)
                orm_failed = subprocess.run(['bash', str(script)], env={**env, 'FAIL_RECOVERY': '0', 'FAIL_ORM': '1'},
                                            text=True, capture_output=True)
                self.assertEqual(orm_failed.returncode, 10, orm_failed.stderr)
                self.assertIn('ORM recovery proof rejected', orm_failed.stderr)
                for name in ARTIFACT_NAMES:
                    self.assertEqual((overrides / (name + '.next')).read_text(), 'previous candidate ' + name)
                    self.assertEqual((overrides / name).read_text(), 'live ' + name)
                    (overrides / (name + '.next')).unlink()
                env['PRIOR_CANDIDATES'] = '{}'
                failed_fresh = subprocess.run(['bash', str(script)], env={**env, 'FAIL_RECOVERY': '1'},
                                              text=True, capture_output=True)
                self.assertEqual(failed_fresh.returncode, 9, failed_fresh.stderr)
                self.assertEqual(list(overrides.glob('*.next')), [])
                orm_failed_fresh = subprocess.run(['bash', str(script)],
                                                  env={**env, 'FAIL_RECOVERY': '0', 'FAIL_ORM': '1'},
                                                  text=True, capture_output=True)
                self.assertEqual(orm_failed_fresh.returncode, 10, orm_failed_fresh.stderr)
                self.assertEqual(list(overrides.glob('*.next')), [])
                (root / 'orm.json').unlink()
                prepared = subprocess.run(['bash', str(script)], env={**env, 'FAIL_RECOVERY': '0'},
                                          text=True, capture_output=True)
                self.assertEqual(prepared.returncode, 0, prepared.stderr)
                orm_command = json.loads((root / 'orm.json').read_text())
                self.assertIn('--rm', orm_command)
                self.assertEqual(orm_command[orm_command.index('--network') + 1], 'none')
                self.assertEqual(orm_command[orm_command.index('--entrypoint') + 1], 'python3')
                self.assertEqual(orm_command[-3:], ['fixture-image', '/tests/test_patch_session_recovery.py', '--orm'])
                self.assertTrue(any('target=/tests' in argument and ('readonly' in argument or 'ro' in argument.split(','))
                                    for argument in orm_command))
                self.assertEqual({path.name for path in overrides.glob('*.next')},
                                 {name + '.next' for name in ARTIFACT_NAMES})
                for name in ARTIFACT_NAMES:
                    self.assertEqual((overrides / name).read_text(), 'live ' + name)
                    if name != 'corresponding-source.json':
                        self.assertEqual((overrides / (name + '.next')).read_text(), 'prepared ' + name)
                self.assertEqual(json.loads((overrides / 'corresponding-source.json.next').read_text())['license'], 'AGPL-3.0')

    def test_release_targets_selected_live_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            home = root / 'home'
            socket_path = home / '.colima/default/docker.sock'
            socket_path.parent.mkdir(parents=True)
            docker_socket = socket.socket(socket.AF_UNIX)
            docker_socket.bind(str(socket_path))
            try:
                deploy = home / 'fitness-wger'
                overrides = deploy / 'overrides'
                config = deploy / 'config'
                overrides.mkdir(parents=True)
                config.mkdir()
                (deploy / 'compose.yaml').write_text('services: {}\n')
                (config / 'private.env').write_text('POSTGRES_USER=fitness_wger\nPOSTGRES_DB=fitness_wger\n')
                for name in ('settings-main.py', 'manager-urls.py'):
                    (overrides / name).write_text(f'old {name}\n')
                for name in ARTIFACT_NAMES[1:]:
                    (overrides / f'{name}.next').write_text(f'new {name}\n')
                    if name in {'history-overview.html', 'api-key.html', 'pdf.py', *RECOVERY_TARGETS}:
                        (overrides / name).write_text(f'old {name}\n')
                bundle = b'new graph code\n//# sourceMappingURL=main.js.map\n'
                (overrides / 'react-main.js.next').write_bytes(bundle)
                writer, history = root / 'writer.lock', root / 'history.lock'
                writer.touch(); history.touch()
                docker_log = root / 'docker.jsonl'
                binary = root / 'bin/docker'
                binary.parent.mkdir()
                binary.write_text('''#!/usr/bin/env python3
import hashlib, json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["DOCKER_LOG"]).open("a") as log: log.write(json.dumps(args) + "\\n")
proxy = Path(os.environ['DOCKER_LOG']).with_suffix('.proxy')
if 'up' in args and ('web' in args or 'powersync' in args): proxy.write_text('stale')
if args[-3:] == ['nginx', '-s', 'reload']:
    proxy.write_text('ready')
    proxy.with_suffix('.warming').touch()
fault=os.environ.get('RELEASE_FAULT','')
if fault=='double-storage' and 'exec(sys.stdin.read())' in ' '.join(args):
    print('storage setup rejected',file=sys.stderr);sys.exit(7)
if fault.startswith('double') and 'migrate' in args and '--check' not in args:
    proxy.with_suffix('.rollback-failed').unlink(missing_ok=True)
    print('release migration rejected',file=sys.stderr);sys.exit(9)
if fault.startswith('double') and 'up' in args and any('compose.rollback.yaml' in arg for arg in args) and not proxy.with_suffix('.rollback-failed').exists():
    proxy.with_suffix('.rollback-failed').touch()
    print('rollback recreate rejected',file=sys.stderr);sys.exit(8)
if fault=='double-dead-sync' and 'up' in args and args[-1]=='powersync':proxy.with_suffix('.dead-sync').touch()
state_path = proxy.with_suffix('.images')
state = json.loads(state_path.read_text()) if state_path.exists() else {}
if 'tag' in args: state[args[-1]] = args[-2]
if 'up' in args:
    override = next((Path(arg) for arg in args if 'compose.rollback.yaml' in arg), None)
    recorded = {}
    if override:
        service = None
        for line in override.read_text().splitlines():
            if line.startswith('  ') and not line.startswith('    '): service = line.strip().rstrip(':')
            elif line.strip().startswith('image:'): recorded[service] = line.split('image:', 1)[1].strip()
    for service in ('web','celery_worker','celery_beat','powersync'):
        if service in args:
            image = recorded.get(service, 'image-id-powersync' if service == 'powersync' else 'mutable-' + service)
            state[service] = state.get(image, image)
            if fault in ('proof-wrong-image', 'double-wrong-image') and service == 'celery_worker': state[service] = 'wrong-image'
    state_path.write_text(json.dumps(state))
elif 'tag' in args: state_path.write_text(json.dumps(state))
if "compose" in args and "ps" in args and "-q" in args:
    services = args[args.index("-q") + 1:]
    if len(services) > 1: services = sorted(services)
    if fault in ('proof-missing-container', 'proof-multiple-containers') and 'web' in services:
        print('' if fault == 'proof-missing-container' else 'id-web\\nid-other');sys.exit(0)
    print("\\n".join({"web":"id-web", "celery_worker":"id-worker", "celery_beat":"id-beat", "powersync":"id-powersync"}[service] for service in services))
elif "inspect" in args:
    template, target = args[args.index("-f") + 1], args[-1]
    service = {'id-web':'web','id-worker':'celery_worker','id-beat':'celery_beat','id-powersync':'powersync'}[target]
    if template == "{{.Image}}": print(state.get(service, "image-" + target) if fault.startswith(('proof-', 'double')) else "image-" + target)
    elif template == "{{.State.Status}}": print('exited' if (target=='id-powersync' and proxy.with_suffix('.dead-sync').exists()) or (fault=='proof-wrong-state' and service=='celery_worker' and state) else 'running')
    else: print("healthy")
elif any("stored_name" in arg for arg in args):
    manifest = json.loads(Path(os.environ["STATIC_MANIFEST"]).read_text())
    print(manifest["paths"]["node/@wger-project/react-components/build/main.js"])
elif "compose" in args and "cp" in args:
    index = args.index("cp")
    source, destination = args[index + 1:index + 3]
    relative = source.removeprefix("nginx:/wger/static/")
    Path(destination).write_bytes((Path(os.environ["STATIC_ROOT"]) / relative).read_bytes())
elif 'showmigrations' in args or any('MigrationRecorder' in arg for arg in args):
    restored = any('compose.rollback.yaml' in arg for arg in args)
    rows = [['manager','0029'], ['exercises','0040']]
    if restored and fault.startswith('proof-'):
        rows.reverse()
        if fault == 'proof-missing': rows.pop()
        if fault == 'proof-extra': rows.append(['removed_app','0001_absent_from_image'])
        if fault == 'proof-duplicate': rows.append(rows[0])
        if fault == 'proof-malformed-row': rows.append(['manager', 30])
    if 'showmigrations' in args:
        if fault.startswith('proof-'): print('2026-09-25 12:00:0' + str(int(restored)) + ' AXES startup')
        print('\\n'.join('[X] ' + str(app) + '.' + str(name) for app, name in rows))
    else:
        print('2026-09-25 12:00:0' + str(int(restored)) + ' AXES startup')
        print('WGER_SCHEMA_BEGIN')
        print('invalid-json' if restored and fault == 'proof-malformed-json' else json.dumps(rows))
        if not (restored and fault == 'proof-missing-end'): print('WGER_SCHEMA_END')
elif fault == 'proof-pending' and 'migrate' in args and '--check' in args:
    print('pending migration', file=sys.stderr);sys.exit(1)
elif any('WGER_RECOVERY_READY' in arg for arg in args):
    service = args[args.index('-T') + 1]
    if fault == 'import-' + service:
        print('recovery import rejected: ' + service, file=sys.stderr);sys.exit(1)
    print('AXES startup')
    print('WGER_RECOVERY_READY')
elif "pg_dump" in args: sys.stdout.buffer.write(b"database")
elif "config" in args and "--format" in args:
    services={name:{} for name in ('web','celery_worker','celery_beat','powersync','db','cache','nginx')}
    services['web']['volumes']=[
        {'type':'bind','source':str(Path(os.environ['WGER_DEPLOY_DIR'])/'overrides/history-overview.html'),'target':'/home/wger/src/wger/exercises/templates/history/overview.html'},
        {'type':'bind','source':str(Path(os.environ['WGER_DEPLOY_DIR'])/'overrides/api-key.html'),'target':'/home/wger/src/wger/core/templates/user/api_key.html'},
        {'type':'bind','source':str(Path(os.environ['WGER_DEPLOY_DIR'])/'overrides/pdf.py'),'target':'/home/wger/src/wger/utils/pdf.py'},
        {'type':'bind','source':str(Path(os.environ['WGER_DEPLOY_DIR'])/'overrides/settings-main.py'),'target':'/home/wger/src/settings/main.py'},
        {'type':'bind','source':str(Path(os.environ['WGER_DEPLOY_DIR'])/'overrides/manager-urls.py'),'target':'/home/wger/src/wger/manager/urls.py'},
    ]
    recovery_targets = json.loads(os.environ['RECOVERY_TARGETS'])
    for service in ('web', 'celery_worker', 'celery_beat'):
        volumes = services[service].setdefault('volumes', [])
        if service != 'web':
            volumes.append({'type':'bind','source':str(Path(os.environ['WGER_DEPLOY_DIR'])/'overrides/settings-main.py'),'target':'/home/wger/src/settings/main.py','read_only':True})
        volumes.extend({'type':'bind','source':str(Path(os.environ['WGER_DEPLOY_DIR'])/'overrides'/name),'target':'/home/wger/src/'+target,'read_only':True} for name, target in recovery_targets.items() if any('candidate/compose.yaml' in arg for arg in args) or (Path(os.environ['WGER_DEPLOY_DIR'])/'overrides'/name).exists())
    if any('candidate/compose.yaml' in arg for arg in args) or (Path(os.environ['WGER_DEPLOY_DIR'])/'formats/en_AU/formats.py').exists():
        services['web']['volumes'].append({'type':'bind','source':str(Path(os.environ['WGER_DEPLOY_DIR'])/'formats/en_AU/formats.py'),'target':'/home/wger/src/wger/formats/en_AU/formats.py'})
    if fault=='missing':services['web']['volumes'].append({'type':'bind','source':str(Path(os.environ['WGER_DEPLOY_DIR'])/'missing.py'),'target':'/home/wger/src/settings/main.py'})
    for arg in args:
        if 'compose.rollback.yaml' in arg and 'build: null' in Path(arg).read_text():
            print('services.web.build must be a string',file=sys.stderr);sys.exit(1)
    print(json.dumps({'services':services}))
elif "sha256sum" in args:
    path = Path(os.environ["STATIC_ROOT"]) / Path(args[-1]).relative_to("/wger/static")
    print(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + args[-1])
''')
                binary.chmod(0o755)
                served_name = 'node/@wger-project/react-components/build/main.123456789abc.js'
                served_file = root / 'static' / served_name
                served_file.parent.mkdir(parents=True)
                served_file.write_bytes(bundle.replace(b'main.js.map', b'main.js.92e5bc28799b.map'))
                class Handler(http.server.BaseHTTPRequestHandler):
                    def do_GET(self):
                        if docker_log.with_suffix('.proxy').read_text() != 'ready':
                            self.send_error(502, 'nginx retained old upstream');return
                        warming = docker_log.with_suffix('.warming')
                        if warming.exists():
                            warming.unlink()
                            self.send_error(503, 'nginx reload is pending');return
                        self.send_response(200)
                        self.send_header('Last-Modified', 'Fri, 25 Sep 2026 00:00:00 GMT')
                        self.end_headers()
                        if self.path.startswith('/static/'):
                            self.wfile.write(b'stale public cache' if (root / 'stale-http').exists() else served_file.read_bytes())
                        else:
                            self.wfile.write(b'\xff' if (root / 'malformed-http').exists() else ('<script src="/static/' + served_name + '"></script>').encode())
                    def log_message(self, *args):
                        pass
                server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self.addCleanup(server.server_close)
                self.addCleanup(server.shutdown)
                manifest = root / 'staticfiles.json'
                manifest.write_text(json.dumps({'paths': {'node/@wger-project/react-components/build/main.js': served_name}}))
                env = {**os.environ, 'HOME': str(home), 'PATH': str(binary.parent) + ':' + os.environ['PATH'],
                       'DOCKER_LOG': str(docker_log), 'WGER_DEPLOY_DIR': str(deploy),
                       'WGER_WRITER_LOCK': str(writer), 'WGER_HISTORY_LOCK': str(history),
                       'WGER_PUBLIC_URL': 'http://127.0.0.1:' + str(server.server_port),
                       'RECOVERY_TARGETS': json.dumps(RECOVERY_TARGETS),
                       'STATIC_ROOT': str(root / 'static'), 'STATIC_MANIFEST': str(manifest)}
                result = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env=env,
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((overrides / 'react-main.js').read_bytes(), bundle)
                proof = json.loads(result.stdout.splitlines()[-1])['live_proof']
                self.assertEqual(proof['expected_sha256'], hashlib.sha256(bundle).hexdigest())
                self.assertEqual(proof['normalized_sha256'], proof['expected_sha256'])
                self.assertNotEqual(proof['served_sha256'], proof['expected_sha256'])
                self.assertTrue(proof['date'])
                self.assertEqual(proof['last_modified'], 'Fri, 25 Sep 2026 00:00:00 GMT')
                for name in ARTIFACT_NAMES[1:]:
                    self.assertEqual((overrides / name).read_text(), f'new {name}\n')
                commands = [json.loads(line) for line in docker_log.read_text().splitlines()]
                self.assertTrue(any(str(deploy / 'compose.yaml') in command for command in map(' '.join, commands)))
                self.assertTrue(any('cp nginx:/wger/static/' + served_name in ' '.join(command) for command in commands))
                self.assertFalse(any('build' in command for command in commands))
                self.assertTrue(all('--no-build' in command for command in commands if 'up' in command))
                web_up = next(i for i, command in enumerate(commands) if 'up' in command and 'web' in command)
                migrate = next(i for i, command in enumerate(commands) if 'migrate' in command and '--no-input' in command)
                checked = next(i for i, command in enumerate(commands) if 'migrate' in command and '--check' in command)
                self.assertLess(web_up, migrate)
                self.assertLess(migrate, checked)
                for service in ('celery_worker', 'celery_beat'):
                    started = next(i for i, command in enumerate(commands) if 'up' in command and service in command)
                    self.assertLess(checked, started)
                imports = [(i, command[command.index('-T') + 1]) for i, command in enumerate(commands)
                           if any('WGER_RECOVERY_READY' in arg for arg in command)]
                self.assertEqual([service for _, service in imports], ['web', 'celery_worker', 'celery_beat'])
                self.assertTrue(all(i > checked for i, _ in imports))
                bundle_check = next(i for i, command in enumerate(commands) if any('stored_name' in arg for arg in command))
                self.assertLess(imports[-1][0], bundle_check)
                candidate = root / 'candidate'
                (candidate / 'config').mkdir(parents=True)
                (candidate / 'overrides').mkdir()
                stage_names = ('compose.yaml', 'overrides/settings-main.py', 'overrides/manager-urls.py', 'config/nginx.conf', 'config/powersync.yaml', 'config/sync_rules.yaml', 'formats/en_AU/formats.py')
                for name in stage_names:
                    candidate_path = candidate / name
                    candidate_path.parent.mkdir(parents=True, exist_ok=True)
                    candidate_path.write_text('new ' + name)
                    if name != 'formats/en_AU/formats.py':
                        deploy_path = deploy / name
                        deploy_path.parent.mkdir(parents=True, exist_ok=True)
                        deploy_path.write_text('old ' + name)
                for name in ARTIFACT_NAMES:
                    (candidate / 'overrides' / (name + '.next')).write_bytes((overrides / (name + '.next')).read_bytes())
                private_before = (config / 'private.env').read_bytes()
                staged_env = {**env, 'WGER_SOURCE_DEPLOY': str(candidate)}
                recovery_before = {}
                for index, name in enumerate(RECOVERY_TARGETS):
                    target = overrides / name
                    target.unlink()
                    recovery_before[name] = None if index % 2 == 0 else f'prior deployment {name}\n'.encode()
                    if recovery_before[name] is not None:
                        target.write_bytes(recovery_before[name])

                def assert_recovery_restored():
                    for name, prior in recovery_before.items():
                        with self.subTest(restored_artifact=name):
                            if prior is None:
                                self.assertFalse((overrides / name).exists())
                            else:
                                self.assertEqual((overrides / name).read_bytes(), prior)

                (root / 'stale-http').touch()
                rejected = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env=staged_env, text=True, capture_output=True)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn('web release failed', rejected.stderr)
                for name in stage_names:
                    if name == 'formats/en_AU/formats.py':
                        self.assertFalse((deploy / name).exists())
                    else:
                        self.assertEqual((deploy / name).read_text(), 'old ' + name)
                self.assertEqual((config / 'private.env').read_bytes(), private_before)
                assert_recovery_restored()
                (root / 'stale-http').unlink()
                (root / 'malformed-http').touch()
                (overrides / 'react-main.js').write_bytes(b'prior browser bundle')
                offset = len(docker_log.read_text().splitlines())
                malformed = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env=staged_env, text=True, capture_output=True)
                self.assertNotEqual(malformed.returncode, 0)
                self.assertIn('web release failed', malformed.stderr)
                for name in stage_names:
                    if name == 'formats/en_AU/formats.py':
                        self.assertFalse((deploy / name).exists())
                    else:
                        self.assertEqual((deploy / name).read_text(), 'old ' + name)
                self.assertEqual((overrides / 'react-main.js').read_bytes(), b'prior browser bundle')
                self.assertEqual((config / 'private.env').read_bytes(), private_before)
                assert_recovery_restored()
                recovery = [json.loads(line) for line in docker_log.read_text().splitlines()[offset:]]
                self.assertTrue(any('pg_restore' in command for command in recovery))
                for service in ('web', 'celery_worker', 'celery_beat', 'powersync'):
                    self.assertTrue(any('up' in command and service in command and any('compose.rollback.yaml' in arg for arg in command) for command in recovery))
                self.assertEqual(docker_log.with_suffix('.proxy').read_text(), 'ready')
                (root / 'malformed-http').unlink()
                (root / 'stale-http').touch()
                for fault, error in (
                    ('proof-noise', None),
                    ('proof-missing', 'not the prior database schema'),
                    ('proof-extra', 'not the prior database schema'),
                    ('proof-duplicate', 'invalid applied migration proof'),
                    ('proof-malformed-row', 'invalid applied migration proof'),
                    ('proof-malformed-json', 'invalid applied migration proof'),
                    ('proof-missing-end', 'invalid applied migration proof'),
                    ('proof-pending', 'pending migration'),
                    ('proof-wrong-image', 'prior image/running state: celery_worker'),
                    ('proof-wrong-state', 'prior image/running state: celery_worker'),
                    ('proof-missing-container', 'exactly one running container: web'),
                    ('proof-multiple-containers', 'exactly one running container: web'),
                ):
                    with self.subTest(rollback_proof=fault):
                        docker_log.with_suffix('.images').unlink(missing_ok=True)
                        outcome = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env={**staged_env, 'RELEASE_FAULT': fault}, text=True, capture_output=True)
                        self.assertNotEqual(outcome.returncode, 0)
                        if error:
                            self.assertIn(error, outcome.stderr)
                            self.assertNotIn('exact images restored', outcome.stderr)
                        else:
                            self.assertIn('exact images restored', outcome.stderr)
                            restored_images = json.loads(docker_log.with_suffix('.images').read_text())
                            self.assertEqual({service: restored_images[service] for service in ('web', 'celery_worker', 'celery_beat', 'powersync')},
                                             {'web':'image-id-web', 'celery_worker':'image-id-worker', 'celery_beat':'image-id-beat', 'powersync':'image-id-powersync'})
                docker_log.with_suffix('.images').unlink(missing_ok=True)
                (root / 'stale-http').unlink()
                for service in ('celery_worker', 'celery_beat'):
                    with self.subTest(import_failure=service):
                        offset = len(docker_log.read_text().splitlines())
                        failed = subprocess.run([sys.executable, str(ROOT / 'release_web.py')],
                                                env={**staged_env, 'RELEASE_FAULT': 'import-' + service},
                                                text=True, capture_output=True)
                        self.assertNotEqual(failed.returncode, 0)
                        self.assertIn('recovery import rejected: ' + service, failed.stderr)
                        self.assertIn('exact images restored', failed.stderr)
                        assert_recovery_restored()
                        attempts = [json.loads(line) for line in docker_log.read_text().splitlines()[offset:]]
                        self.assertTrue(any('pg_restore' in command for command in attempts))
                        self.assertTrue(any('migrate' in command and '--no-input' in command for command in attempts))
                        self.assertTrue(any('migrate' in command and '--check' in command and
                                            not any('compose.rollback.yaml' in arg for arg in command) for command in attempts))
                        self.assertTrue(all('--no-build' in command for command in attempts if 'up' in command))
                        self.assertFalse(any('stored_name' in arg for command in attempts for arg in command))
                        for restored in ('web', 'celery_worker', 'celery_beat'):
                            self.assertTrue(any('up' in command and restored in command and
                                                any('compose.rollback.yaml' in arg for arg in command) for command in attempts))
                (deploy / 'settings-main.py').mkdir()
                staged = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env=staged_env, text=True, capture_output=True)
                self.assertEqual(staged.returncode, 0, staged.stderr)
                self.assertTrue((deploy / 'settings-main.py').is_dir())
                for name in stage_names:
                    self.assertEqual((deploy / name).read_text(), 'new ' + name)
                for name in RECOVERY_TARGETS:
                    self.assertEqual((overrides / name).read_text(), f'new {name}\n')
                self.assertEqual((config / 'private.env').read_bytes(), private_before)
                offset = len(docker_log.read_text().splitlines())
                refused = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env={**staged_env,'RELEASE_FAULT':'missing'}, text=True,capture_output=True)
                self.assertNotEqual(refused.returncode,0)
                self.assertIn('bind source missing or wrong type',refused.stderr)
                refused_commands=[json.loads(line) for line in docker_log.read_text().splitlines()[offset:]]
                self.assertFalse(any('stop' in command or 'up' in command for command in refused_commands))
                for name in ('settings-main.py', 'manager-urls.py'):
                    target = overrides / name
                    original = target.read_bytes()
                    target.unlink()
                    target.mkdir()
                    offset = len(docker_log.read_text().splitlines())
                    wrong_type = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env=staged_env, text=True, capture_output=True)
                    self.assertNotEqual(wrong_type.returncode, 0)
                    self.assertIn('bind source missing or wrong type', wrong_type.stderr)
                    target.rmdir()
                    other = root / name
                    other.write_bytes(original)
                    target.symlink_to(other)
                    linked = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env=staged_env, text=True, capture_output=True)
                    self.assertNotEqual(linked.returncode, 0)
                    self.assertIn('bind source missing or wrong type', linked.stderr)
                    attempts = [json.loads(line) for line in docker_log.read_text().splitlines()[offset:]]
                    self.assertFalse(any('stop' in command or 'up' in command for command in attempts))
                    target.unlink()
                    target.write_bytes(original)
                docker_log.with_suffix('.images').unlink(missing_ok=True)
                double = subprocess.run([sys.executable,str(ROOT/'release_web.py')],env={**staged_env,'RELEASE_FAULT':'double'},text=True,capture_output=True)
                self.assertNotEqual(double.returncode,0)
                self.assertIn('release migration rejected',double.stderr)
                self.assertIn('rollback recreate rejected',double.stderr)
                self.assertIn('previous compose running; public login 200',double.stderr)
                self.assertEqual(docker_log.with_suffix('.proxy').read_text(),'ready')
                self.assertEqual(json.loads(docker_log.with_suffix('.images').read_text())['celery_worker'], 'image-id-worker')
                docker_log.with_suffix('.images').unlink(missing_ok=True)
                wrong = subprocess.run([sys.executable,str(ROOT/'release_web.py')],env={**staged_env,'RELEASE_FAULT':'double-wrong-image'},text=True,capture_output=True)
                self.assertIn('final restore did not recover prior image/running state: celery_worker', wrong.stderr)
                self.assertNotIn('previous compose running;', wrong.stderr)
                docker_log.with_suffix('.images').unlink(missing_ok=True)
                storage = subprocess.run([sys.executable,str(ROOT/'release_web.py')],env={**staged_env,'RELEASE_FAULT':'double-storage'},text=True,capture_output=True)
                self.assertNotEqual(storage.returncode,0)
                self.assertIn('storage setup rejected',storage.stderr)
                self.assertNotIn('previous compose running;',storage.stderr)
                self.assertEqual(docker_log.with_suffix('.proxy').read_text(),'ready')
                docker_log.with_suffix('.images').unlink(missing_ok=True)
                dead = subprocess.run([sys.executable,str(ROOT/'release_web.py')],env={**staged_env,'RELEASE_FAULT':'double-dead-sync'},text=True,capture_output=True)
                self.assertNotEqual(dead.returncode,0)
                self.assertIn('final restore did not recover prior image/running state: powersync',dead.stderr)
                self.assertNotIn('previous compose running;',dead.stderr)
                docker_log.with_suffix('.dead-sync').unlink()
                served_file.write_text('stale collected browser bundle\n')
                mismatch = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env=env,
                                          text=True, capture_output=True)
                self.assertNotEqual(mismatch.returncode, 0)
                self.assertIn('web release failed', mismatch.stderr)
                logical_name = 'node/@wger-project/react-components/build/main.js'
                manifest.write_text(json.dumps({'paths': {logical_name: logical_name}}))
                plain_file = root / 'static' / logical_name
                plain_file.write_bytes(bundle)
                fallback = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env=env,
                                          text=True, capture_output=True)
                self.assertNotEqual(fallback.returncode, 0)
                self.assertIn('web release failed', fallback.stderr)
            finally:
                docker_socket.close()

    def test_private_env_parser_handles_canonical_unquoted_spaces_without_eval(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from release_env import read_database_environment
        with tempfile.TemporaryDirectory() as directory:
            fixture = pathlib.Path(directory) / 'private.env'
            fixture.write_text('POSTGRES_USER=fitness_wger\nPOSTGRES_DB=fitness_wger\nGUNICORN_CMD_ARGS=--workers 1 --threads 2 --timeout 240\nPOSTGRES_PASSWORD=never-return-this\n')
            self.assertEqual(read_database_environment(fixture), {'POSTGRES_USER': 'fitness_wger', 'POSTGRES_DB': 'fitness_wger'})


    def test_release_refuses_cross_process_history_lockf_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            writer, history = root / 'training-writer.lock', root / '.training-writer.lock'
            writer.touch(); history.touch()
            owner = subprocess.Popen([sys.executable, '-c',
                'import fcntl,sys,time; f=open(sys.argv[1],"a"); fcntl.lockf(f,fcntl.LOCK_EX); print("locked",flush=True); time.sleep(30)', str(history)],
                stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(owner.stdout.readline().strip(), 'locked')
                env = {**os.environ, 'WGER_WRITER_LOCK': str(writer), 'WGER_HISTORY_LOCK': str(history),
                       'WGER_RELEASE_LOCK_DIR': str(root / 'release'),
                       'WGER_DOCKER_HOST': f'unix://{pathlib.Path.home()}/.colima/default/docker.sock'}
                result = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env=env, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('writer/import custody is active', result.stderr)
            finally:
                owner.terminate(); owner.wait(); owner.stdout.close()


if __name__ == '__main__':
    unittest.main()
