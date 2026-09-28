import hashlib
import fcntl
import os
import pathlib
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import http.server
import threading
from contextlib import contextmanager

ROOT = pathlib.Path(__file__).parent
# Retired backend overrides a live deployment may still hold; the old compose (rollback) binds them.
LEGACY_OVERRIDES = ('manager-urls.py', 'manager-log.py', 'manager-0030-workoutlog-cardio-metrics.py',
                    'manager-session-recovery.py', 'manager-models-init.py', 'manager-api-views.py',
                    'manager-tasks.py', 'manager-0031-session-recovery.py',
                    'template.html', 'history-overview.html', 'api-key.html', 'pdf.py')
ARTIFACT_NAMES = ('corresponding-source.json',)
RELEASE_NAMES = (*ARTIFACT_NAMES, 'backend-image.json')
CANDIDATE_COMMIT = '0812ca39a80e82c071c99a54300df73e28668776'
# The browser bundle is the image's own package file; the receipt pins its digest.
BUNDLE = b'new graph code\n//# sourceMappingURL=main.js.map\n'
CANDIDATE = {'tag': 'fitness-wger-backend:' + CANDIDATE_COMMIT, 'image_id': 'sha256:' + 'c' * 64,
             'commit': CANDIDATE_COMMIT, 'app_build_commit': CANDIDATE_COMMIT,
             'frontend': {'main_js_sha256': hashlib.sha256(BUNDLE).hexdigest()}}


@contextmanager
def preparation_fixture():
    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory).resolve()
        patches = root / 'deployment/patches'
        patches.mkdir(parents=True)
        for name in ('prepare-react.sh',):
            (patches / name).write_bytes((ROOT / name).read_bytes())
        overrides = patches.parent / 'overrides'
        overrides.mkdir()
        for name in ARTIFACT_NAMES:
            (overrides / (name + '.next')).write_text('previous candidate ' + name)
            (overrides / name).write_text('live ' + name)
        binary = root / 'bin'
        binary.mkdir()
        fake = f'#!{sys.executable}\n' + '''import json, os, subprocess, sys
from pathlib import Path
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with Path(os.environ['COMMAND_LOG']).open('a') as log:
    log.write(json.dumps([tool, *args]) + '\\n')
if tool == 'docker':
    if 'info' in args: sys.exit(1 if os.environ.get('FAIL_PREFLIGHT') == 'daemon' else 0)
elif tool == 'python3':
    if args[0] == '-c':
        os.execv(os.environ['REAL_PYTHON'], [os.environ['REAL_PYTHON'], *args])
'''
        for name in ('docker', 'curl', 'tar', 'npm', 'python3'):
            command = binary / name
            command.write_text(fake)
            command.chmod(0o755)
        socket_path = root / 'docker.sock'
        env = {**os.environ, 'HOME': str(root), 'PATH': str(binary) + ':' + os.environ['PATH'],
               'WGER_DOCKER_HOST': 'unix://' + str(socket_path), 'REAL_PYTHON': sys.executable,
               'COMMAND_LOG': str(root / 'commands.jsonl')}
        with socket.socket(socket.AF_UNIX) as docker_socket:
            docker_socket.bind(str(socket_path))
            yield root, patches / 'prepare-react.sh', overrides, env


class ReleaseRouteTest(unittest.TestCase):
    def test_compose_writers_run_image_source_with_settings_only(self):
        try:
            import yaml
        except ImportError:
            self.skipTest('PyYAML unavailable; no dependency installed by release tests')
        services = yaml.safe_load((ROOT.parent / 'compose.yaml').read_text())['services']
        for service in ('web', 'celery_worker', 'celery_beat'):
            with self.subTest(service=service):
                mounts = [mount.split(':') for mount in services[service]['volumes']]
                self.assertIn(['./overrides/settings-main.py', '/home/wger/src/settings/main.py', 'ro'], mounts)
                self.assertFalse([mount for mount in mounts if mount[0].startswith('./overrides/manager-')])

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

    def test_wger_axes_whitelists_private_docker_and_tailscale_networks(self):
        # Ported from Cub-HQ/fitness-coach runtime/tests/test_wger_tool.py at
        # f6d3efe32e27353defab8fc9a627094af98bfdbd (same test name).
        import ast
        import ipaddress
        tree = ast.parse((ROOT.parent / 'overrides/settings-main.py').read_text())
        wanted = [node for node in tree.body if
                  isinstance(node, ast.FunctionDef) and node.name == '_never_lock_our_nets' or
                  isinstance(node, ast.Assign) and [getattr(target, 'id', None) for target in node.targets] in (
                      ['_OUR_NETWORKS'], ['AXES_WHITELIST_CALLABLE'])]
        namespace = {'ipaddress': ipaddress}
        exec(compile(ast.Module(body=wanted, type_ignores=[]), '<axes-settings>', 'exec'), namespace)
        allowed = namespace['AXES_WHITELIST_CALLABLE']
        self.assertIs(allowed, namespace['_never_lock_our_nets'])
        request = lambda remote, forwarded='': type('Request', (), {'META': {
            'REMOTE_ADDR': remote, 'HTTP_X_FORWARDED_FOR': forwarded}})()
        for address in ('172.18.0.1', '100.64.0.7', '100.127.255.254', '127.0.0.1'):
            with self.subTest(remote=address):
                self.assertTrue(allowed(request(address)))
        self.assertTrue(allowed(request('172.18.0.1', '8.8.8.8')))
        self.assertTrue(allowed(request('8.8.8.8', '100.64.0.7, 172.18.0.1')))
        for remote, forwarded in (('8.8.8.8', '1.1.1.1'), ('100.128.0.1', ''),
                                  ('8.8.8.8', '1.1.1.1, 100.64.0.7'), ('not-an-ip', 'garbage')):
            with self.subTest(remote=remote, forwarded=forwarded):
                self.assertFalse(allowed(request(remote, forwarded)))

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
        namespace.update({'public_url': 'https://gym.invalid', 'Request': Request, 'urljoin': urljoin, 're': re,
                          'hashlib': hashlib})
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<http_bundle>', 'exec'), namespace)
        login = io.BytesIO(b'<script src="/static/node/@wger-project/react-components/build/main.abc123.js"></script>')
        bundle = io.BytesIO(b'wrong bundle')
        bundle.headers = {'Date': 'today', 'Last-Modified': 'yesterday'}
        namespace['public_response'] = Mock(side_effect=[login, bundle])
        with self.assertRaisesRegex(OSError, 'does not match staged release'):
            namespace['http_bundle'](hashlib.sha256(b'correct bundle').hexdigest())
        self.assertEqual(namespace['public_response'].call_count, 2)

    def test_preparation_stages_provenance_without_touching_images(self):
        with preparation_fixture() as (root, script, overrides, env):
            result = subprocess.run(['bash', str(script)], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = [json.loads(line) for line in (root / 'commands.jsonl').read_text().splitlines()]
            for command in commands:
                if command[0] == 'docker':
                    self.assertEqual(command[1:3], ['-H', env['WGER_DOCKER_HOST']])
            self.assertFalse(any(command[0] in ('curl', 'tar', 'npm') for command in commands))
            self.assertFalse(any(command[0] == 'docker' and set(command) & {'up', 'build', 'run', 'create', 'cp'}
                                 for command in commands))
            self.assertFalse(any(command[0] == 'python3' and command[1] != '-c' for command in commands))

    def test_preparation_rejects_preflight_failures_before_work(self):
        cases = [
            ({'WGER_DOCKER_HOST': ''}, 'Docker endpoint'),
            ({'WGER_DOCKER_HOST': 'unix://'}, 'Docker endpoint'),
            ({'WGER_DOCKER_HOST': 'unix://relative.sock'}, 'Docker endpoint'),
            ({'WGER_DOCKER_HOST': 'tcp://localhost:2375'}, 'Docker endpoint'),
            ({'WGER_DOCKER_HOST': 'unix:///absent-wger-test.sock'}, 'Docker socket'),
            ({'FAIL_PREFLIGHT': 'daemon'}, 'Docker daemon'),
        ]
        for failure, condition in cases:
            with self.subTest(failure=failure), preparation_fixture() as (root, script, overrides, env):
                before = {path.name: path.read_bytes() for path in overrides.iterdir()}
                result = subprocess.run(['bash', str(script)], env={**env, **failure},
                                        text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('preflight failed: ' + condition, result.stderr)
                self.assertEqual({path.name: path.read_bytes() for path in overrides.iterdir()}, before)
                commands = [json.loads(line) for line in (root / 'commands.jsonl').read_text().splitlines()] if (root / 'commands.jsonl').exists() else []
                self.assertFalse(any(command[0] in ('curl', 'tar', 'npm') for command in commands))
                self.assertFalse(any(command[0] == 'python3' and command[1] != '-c' for command in commands))
                self.assertFalse(any('create' in command or 'cp' in command for command in commands))

    def test_preparation_publishes_only_the_corresponding_source_candidate(self):
        with preparation_fixture() as (root, script, overrides, env):
            prepared = subprocess.run(['bash', str(script)], env=env, text=True, capture_output=True)
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            self.assertEqual({path.name for path in overrides.glob('*.next')},
                             {name + '.next' for name in ARTIFACT_NAMES})
            for name in ARTIFACT_NAMES:
                self.assertEqual((overrides / name).read_text(), 'live ' + name)
            source = json.loads((overrides / 'corresponding-source.json.next').read_text())
            pins = dict(re.findall(r'^(BACKEND_COMMIT|FRONTEND_COMMIT)=(\S+)$', script.read_text(), re.MULTILINE))
            self.assertEqual(source['license'], 'AGPL-3.0')
            self.assertEqual(source['server']['commit'], pins['BACKEND_COMMIT'])
            self.assertEqual(source['frontend']['commit'], pins['FRONTEND_COMMIT'])

    def test_release_targets_selected_live_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            home = root / 'home'
            socket_path = home / 'selected-engine/docker.sock'
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
                (overrides / 'settings-main.py').write_text('old settings-main.py\n')
                for name in ARTIFACT_NAMES:
                    (overrides / f'{name}.next').write_text(f'new {name}\n')
                for name in LEGACY_OVERRIDES:
                    (overrides / name).write_text(f'old {name}\n')
                bundle = BUNDLE
                (overrides / 'backend-image.json.next').write_text(json.dumps(CANDIDATE))
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
            default = 'image-id-powersync' if service == 'powersync' else json.loads(os.environ['CANDIDATE'])['image_id']
            image = recorded.get(service, 'other-image' if fault == 'writer-image' and service == 'celery_beat' else default)
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
elif "image" in args and "inspect" in args:
    candidate = json.loads(os.environ['CANDIDATE'])
    commit = 'f' * 40 if fault == 'image-commit' else candidate['commit']
    print(json.dumps({'Id': 'sha256:' + 'd' * 64 if fault == 'image-moved' else candidate['image_id'],
                      'Config': {'Env': ['PATH=/usr/bin', 'APP_BUILD_COMMIT=' + commit],
                                 'Labels': {'org.opencontainers.image.revision': candidate['commit']}}}))
elif "inspect" in args:
    template, target = args[args.index("-f") + 1], args[-1]
    service = {'id-web':'web','id-worker':'celery_worker','id-beat':'celery_beat','id-powersync':'powersync'}[target]
    if template == "{{.Image}}": print(state.get(service, "image-" + target))
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
    for service in ('web','celery_worker','celery_beat'):
        services[service]['image'] = 'fitness-wger-backend:stale' if fault == 'compose-tag' else json.loads(os.environ['CANDIDATE'])['tag']
    services['web']['volumes']=[
        {'type':'bind','source':str(Path(os.environ['WGER_DEPLOY_DIR'])/'overrides/settings-main.py'),'target':'/home/wger/src/settings/main.py'},
    ]
    for service in ('web', 'celery_worker', 'celery_beat'):
        volumes = services[service].setdefault('volumes', [])
        if service != 'web':
            volumes.append({'type':'bind','source':str(Path(os.environ['WGER_DEPLOY_DIR'])/'overrides/settings-main.py'),'target':'/home/wger/src/settings/main.py','read_only':True})
        # Only the prior (old-layout) compose still binds retired backend overrides.
        if not any('candidate/compose.yaml' in arg for arg in args):
            volumes.extend({'type':'bind','source':str(path),'target':'/home/wger/src/legacy/'+path.name,'read_only':True} for path in sorted((Path(os.environ['WGER_DEPLOY_DIR'])/'overrides').glob('manager-*.py')))
    if fault=='missing':services['web']['volumes'].append({'type':'bind','source':str(Path(os.environ['WGER_DEPLOY_DIR'])/'missing.py'),'target':'/home/wger/src/settings/main.py'})
    if fault=='frontend-bind':services['web']['volumes'].append({'type':'bind','source':str(Path(os.environ['WGER_DEPLOY_DIR'])/'overrides/template.html'),'target':'/home/wger/src/node_modules/@wger-project/react-components/build/main.js'})
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
                       'WGER_DOCKER_HOST': 'unix://' + str(socket_path),
                       'DOCKER_LOG': str(docker_log), 'WGER_DEPLOY_DIR': str(deploy),
                       'WGER_WRITER_LOCK': str(writer), 'WGER_HISTORY_LOCK': str(history),
                       'WGER_PUBLIC_URL': 'http://127.0.0.1:' + str(server.server_port),
                       'CANDIDATE': json.dumps(CANDIDATE),
                       'STATIC_ROOT': str(root / 'static'), 'STATIC_MANIFEST': str(manifest)}
                result = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env=env,
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((overrides / 'react-main.js').exists(), 'release must not stage a browser bundle overlay')
                proof = json.loads(result.stdout.splitlines()[-1])['live_proof']
                self.assertEqual(proof['expected_sha256'], hashlib.sha256(bundle).hexdigest())
                self.assertEqual(proof['normalized_sha256'], proof['expected_sha256'])
                self.assertNotEqual(proof['served_sha256'], proof['expected_sha256'])
                self.assertTrue(proof['date'])
                self.assertEqual(proof['last_modified'], 'Fri, 25 Sep 2026 00:00:00 GMT')
                for name in ARTIFACT_NAMES:
                    self.assertEqual((overrides / name).read_text(), f'new {name}\n')
                commands = [json.loads(line) for line in docker_log.read_text().splitlines()]
                for command in commands:
                    self.assertEqual(command[:2], ['-H', env['WGER_DOCKER_HOST']])
                self.assertTrue(any(str(deploy / 'compose.yaml') in command for command in map(' '.join, commands)))
                self.assertTrue(any('cp nginx:/wger/static/' + served_name in ' '.join(command) for command in commands))
                self.assertFalse(any('build' in command for command in commands))
                self.assertTrue(all('--no-build' in command for command in commands if 'up' in command))
                web_up = next(i for i, command in enumerate(commands) if 'up' in command and 'web' in command)
                migrate = next(i for i, command in enumerate(commands) if 'migrate' in command and '--no-input' in command)
                checked = next(i for i, command in enumerate(commands) if 'migrate' in command and '--check' in command)
                # `up` returns at creation, not readiness. Migrating before the health
                # wait attaches the exec to the container being replaced, which dies
                # with 137 when the recreate kills it.
                health = next(i for i, command in enumerate(commands) if 'inspect' in command and any('Health' in arg for arg in command))
                self.assertLess(web_up, health)
                self.assertLess(health, migrate)
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
                stage_names = ('compose.yaml', 'overrides/settings-main.py', 'config/nginx.conf', 'config/powersync.yaml', 'config/sync_rules.yaml')
                # A first-release config file must return to absent on rollback.
                absent_config = 'config/sync_rules.yaml'
                for name in stage_names:
                    candidate_path = candidate / name
                    candidate_path.parent.mkdir(parents=True, exist_ok=True)
                    candidate_path.write_text('new ' + name)
                    if name != absent_config:
                        deploy_path = deploy / name
                        deploy_path.parent.mkdir(parents=True, exist_ok=True)
                        deploy_path.write_text('old ' + name)
                for name in RELEASE_NAMES:
                    (candidate / 'overrides' / (name + '.next')).write_bytes((overrides / (name + '.next')).read_bytes())
                private_before = (config / 'private.env').read_bytes()
                staged_env = {**env, 'WGER_SOURCE_DEPLOY': str(candidate)}
                legacy_before = {}
                for index, name in enumerate(LEGACY_OVERRIDES):
                    target = overrides / name
                    target.unlink()
                    legacy_before[name] = None if index % 2 == 0 else f'prior deployment {name}\n'.encode()
                    if legacy_before[name] is not None:
                        target.write_bytes(legacy_before[name])

                def assert_recovery_restored():
                    # Retired overrides are never staged, replaced or removed: old-layout rollback keeps them.
                    for name, prior in legacy_before.items():
                        with self.subTest(legacy_artifact=name):
                            if prior is None:
                                self.assertFalse((overrides / name).exists())
                            else:
                                self.assertEqual((overrides / name).read_bytes(), prior)

                (root / 'stale-http').touch()
                rejected = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env=staged_env, text=True, capture_output=True)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn('web release failed', rejected.stderr)
                for name in stage_names:
                    if name == absent_config:
                        self.assertFalse((deploy / name).exists())
                    else:
                        self.assertEqual((deploy / name).read_text(), 'old ' + name)
                self.assertEqual((config / 'private.env').read_bytes(), private_before)
                assert_recovery_restored()
                (root / 'stale-http').unlink()
                (root / 'malformed-http').touch()
                offset = len(docker_log.read_text().splitlines())
                malformed = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env=staged_env, text=True, capture_output=True)
                self.assertNotEqual(malformed.returncode, 0)
                self.assertIn('web release failed', malformed.stderr)
                for name in stage_names:
                    if name == absent_config:
                        self.assertFalse((deploy / name).exists())
                    else:
                        self.assertEqual((deploy / name).read_text(), 'old ' + name)
                self.assertFalse((overrides / 'react-main.js').exists())
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
                # The writers' image is resolved from the receipt before any Docker mutation:
                # a moved tag, wrong build commit, stale compose tag or absent receipt refuses.
                receipt = candidate / 'overrides/backend-image.json.next'
                for fault, content, error in (
                    ('image-moved', None, 'no longer resolves to receipt image'),
                    ('image-commit', None, 'no longer resolves to receipt image'),
                    ('compose-tag', None, 'is not the receipt candidate'),
                    ('frontend-bind', None, 'bind overrides the image frontend package'),
                    ('', '{}', 'receipt is missing or malformed'),
                    ('', json.dumps({**CANDIDATE, 'app_build_commit': 'f' * 40}), 'receipt is missing or malformed'),
                    ('', json.dumps({**CANDIDATE, 'tag': 'fitness-wger-backend:latest'}), 'receipt is missing or malformed'),
                    ('', json.dumps({key: value for key, value in CANDIDATE.items() if key != 'frontend'}), 'receipt is missing or malformed'),
                    ('', json.dumps({**CANDIDATE, 'frontend': {'main_js_sha256': 'upstream'}}), 'receipt is missing or malformed'),
                ):
                    with self.subTest(candidate_refusal=fault or content):
                        if content is not None:
                            receipt.write_text(content)
                        offset = len(docker_log.read_text().splitlines())
                        refused = subprocess.run([sys.executable, str(ROOT / 'release_web.py')],
                                                 env={**staged_env, 'RELEASE_FAULT': fault}, text=True, capture_output=True)
                        self.assertNotEqual(refused.returncode, 0)
                        self.assertIn(error, refused.stderr)
                        attempts = [json.loads(line) for line in docker_log.read_text().splitlines()[offset:]]
                        self.assertFalse(any(verb in command for command in attempts for verb in ('stop', 'up', 'tag', 'pg_dump')))
                        receipt.write_text(json.dumps(CANDIDATE))
                docker_log.with_suffix('.images').unlink(missing_ok=True)
                wrong_writer = subprocess.run([sys.executable, str(ROOT / 'release_web.py')],
                                              env={**staged_env, 'RELEASE_FAULT': 'writer-image'}, text=True, capture_output=True)
                self.assertNotEqual(wrong_writer.returncode, 0)
                self.assertIn('writer is not running the receipt candidate image: celery_beat', wrong_writer.stderr)
                self.assertIn('exact images restored', wrong_writer.stderr)
                assert_recovery_restored()
                (deploy / 'settings-main.py').mkdir()
                staged = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env=staged_env, text=True, capture_output=True)
                self.assertEqual(staged.returncode, 0, staged.stderr)
                self.assertEqual(json.loads(staged.stdout.splitlines()[-1])['backend_image'],
                                 {key: CANDIDATE[key] for key in ('tag', 'image_id', 'commit')})
                self.assertEqual(json.loads((overrides / 'backend-image.json').read_text()), CANDIDATE)
                self.assertEqual(json.loads(docker_log.with_suffix('.images').read_text())['celery_beat'], CANDIDATE['image_id'])
                self.assertTrue((deploy / 'settings-main.py').is_dir())
                for name in stage_names:
                    self.assertEqual((deploy / name).read_text(), 'new ' + name)
                assert_recovery_restored()
                self.assertEqual((config / 'private.env').read_bytes(), private_before)
                offset = len(docker_log.read_text().splitlines())
                refused = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env={**staged_env,'RELEASE_FAULT':'missing'}, text=True,capture_output=True)
                self.assertNotEqual(refused.returncode,0)
                self.assertIn('bind source missing or wrong type',refused.stderr)
                refused_commands=[json.loads(line) for line in docker_log.read_text().splitlines()[offset:]]
                self.assertFalse(any('stop' in command or 'up' in command for command in refused_commands))
                for name in ('settings-main.py',):
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
                for line in docker_log.read_text().splitlines():
                    self.assertEqual(json.loads(line)[:2], ['-H', env['WGER_DOCKER_HOST']])
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

    def test_release_refuses_malformed_public_origin_before_any_docker_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            writer, history = root / 'writer.lock', root / 'history.lock'
            writer.touch(); history.touch()
            (root / 'overrides').mkdir()
            for name in RELEASE_NAMES:
                (root / 'overrides' / (name + '.next')).write_text('staged ' + name)
            (root / 'config').mkdir()
            private_env = root / 'config/private.env'
            docker_log = root / 'docker.log'
            binary = root / 'bin/docker'
            binary.parent.mkdir()
            binary.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(docker_log)!r}).touch()\nraise SystemExit(99)\n')
            binary.chmod(0o755)
            socket_path = root / 'docker.sock'
            env = {**os.environ, 'WGER_DEPLOY_DIR': str(root), 'WGER_DOCKER_HOST': 'unix://' + str(socket_path),
                   'PATH': str(binary.parent) + ':' + os.environ['PATH'],
                   'WGER_WRITER_LOCK': str(writer), 'WGER_HISTORY_LOCK': str(history)}
            env.pop('WGER_PUBLIC_URL', None)
            database = 'POSTGRES_USER=fitness_wger\nPOSTGRES_DB=fitness_wger\n'

            def release(site_lines, override=None):
                private_env.write_text(database + ''.join(line + '\n' for line in site_lines))
                docker_log.unlink(missing_ok=True)
                run_env = dict(env) if override is None else {**env, 'WGER_PUBLIC_URL': override}
                return subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env=run_env,
                                      text=True, capture_output=True)

            malformed = ('https://gym.example # live', 'https://gym example', 'https://gym.example:notaport',
                         'https://:8098', 'http://?x', 'https://gym.example:99999', 'https://gym.example:0',
                         'https://gym.example:', 'https://', 'ftp://gym.example', 'gym.example:8098',
                         'https://user:pass@gym.example', 'https://gym.example/app', 'https://gym.example/?x=1',
                         'https://gym.example#top', 'https://gym.example\t', 'https://gym.exa\x7fmple',
                         'https://gym_example', 'https://gym.exämple', 'https://gym%2eexample',
                         'https://[::1', '"https://gym.example"', 'https://REQUIRED_VERIFIED_TAILNET_HOST', ' ')
            with socket.socket(socket.AF_UNIX) as docker_socket:
                docker_socket.bind(str(socket_path))
                for value in malformed:
                    for source in ('WGER_PUBLIC_URL', 'SITE_URL'):
                        with self.subTest(value=value, source=source):
                            if source == 'SITE_URL':
                                result = release(['SITE_URL=' + value])
                            else:
                                # A bad override is refused, never replaced by a valid SITE_URL.
                                result = release(['SITE_URL=https://gym.example:8098'], override=value)
                            self.assertNotEqual(result.returncode, 0)
                            self.assertIn('must name the gym public origin', result.stderr)
                            self.assertFalse(docker_log.exists(), 'docker called before origin refusal')
                for lines in ([], ['SITE_URL=https://a.example', 'SITE_URL=https://b.example'], ['export SITE_URL=https://gym.example']):
                    with self.subTest(site_lines=lines):
                        result = release(lines)
                        self.assertIn('SITE_URL exactly once', result.stderr)
                        self.assertFalse(docker_log.exists())
                for lines, override in ((['SITE_URL=https://gym.example:8098'], None),
                                        (['SITE_URL=https://gym.example:8098/'], ''),
                                        (['SITE_URL=http://127.0.0.1:8000'], None),
                                        (['SITE_URL=https://REQUIRED_VERIFIED_TAILNET_HOST'], 'https://gym.example'),
                                        ([], 'http://[::1]:8098/')):
                    with self.subTest(site_lines=lines, override=override):
                        result = release(lines, override)
                        self.assertNotIn('public origin', result.stderr)
                        self.assertTrue(docker_log.exists(), 'valid origin must reach the first docker call')


    def test_release_validates_selected_endpoint_before_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            writer, history = root / 'writer.lock', root / 'history.lock'
            writer.touch(); history.touch()
            default_socket = root / '.colima/default/docker.sock'
            default_socket.parent.mkdir(parents=True)
            marker = root / 'candidate.next'
            marker.write_bytes(b'untouched candidate')
            docker_log = root / 'docker.log'
            binary = root / 'bin/docker'
            binary.parent.mkdir()
            binary.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(docker_log)!r}).touch()\nraise SystemExit(99)\n')
            binary.chmod(0o755)
            env = {**os.environ, 'HOME': str(root), 'WGER_DEPLOY_DIR': str(root),
                   'PATH': str(binary.parent) + ':' + os.environ['PATH'],
                   'WGER_WRITER_LOCK': str(writer), 'WGER_HISTORY_LOCK': str(history)}
            env.pop('WGER_DOCKER_HOST', None)
            with socket.socket(socket.AF_UNIX) as docker_socket:
                docker_socket.bind(str(default_socket))
                for endpoint, cause in (
                    ('', 'Docker endpoint'),
                    ('tcp://localhost:2375', 'Docker endpoint'),
                    ('unix://', 'Docker endpoint'),
                    ('unix://relative.sock', 'Docker endpoint'),
                    ('unix://' + str(root / 'missing.sock'), 'Docker socket'),
                    ('unix://' + str(marker), 'Docker socket'),
                ):
                    with self.subTest(endpoint=endpoint):
                        result = subprocess.run([sys.executable, str(ROOT / 'release_web.py')],
                                                env={**env, 'WGER_DOCKER_HOST': endpoint},
                                                text=True, capture_output=True)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(cause, result.stderr)
                        self.assertEqual(marker.read_bytes(), b'untouched candidate')
                        self.assertFalse((root / 'overrides').exists())
                        self.assertFalse(docker_log.exists())
                default = subprocess.run([sys.executable, str(ROOT / 'release_web.py')],
                                         env=env, text=True, capture_output=True)
                self.assertNotEqual(default.returncode, 0)
                self.assertIn('missing staged override', default.stderr)

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
