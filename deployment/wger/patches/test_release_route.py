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


class ReleaseRouteTest(unittest.TestCase):
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
                for name in ('template.html', 'history-overview.html', 'api-key.html', 'pdf.py', 'corresponding-source.json'):
                    (overrides / f'{name}.next').write_text(f'new {name}\n')
                    if name in {'history-overview.html', 'api-key.html', 'pdf.py'}:
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
if args[-3:] == ['nginx', '-s', 'reload']: proxy.write_text('ready')
fault=os.environ.get('RELEASE_FAULT','')
if fault=='double-storage' and 'exec(sys.stdin.read())' in ' '.join(args):
    print('storage setup rejected',file=sys.stderr);sys.exit(7)
if fault.startswith('double') and 'migrate' in args:
    print('release migration rejected',file=sys.stderr);sys.exit(9)
if fault.startswith('double') and 'up' in args and any('compose.rollback.yaml' in arg for arg in args):
    print('rollback recreate rejected',file=sys.stderr);sys.exit(8)
if fault=='double-dead-sync' and 'up' in args and args[-1]=='powersync':proxy.with_suffix('.dead-sync').touch()
if "compose" in args and "ps" in args and "-q" in args:
    services = args[args.index("-q") + 1:]
    print("\\n".join({"web":"id-web", "celery_worker":"id-worker", "celery_beat":"id-beat", "powersync":"id-powersync"}[service] for service in services))
elif "inspect" in args:
    template, target = args[args.index("-f") + 1], args[-1]
    if template == "{{.Image}}": print("image-" + target)
    elif template == "{{.State.Status}}": print('exited' if target=='id-powersync' and proxy.with_suffix('.dead-sync').exists() else 'running')
    else: print("healthy")
elif any("stored_name" in arg for arg in args):
    manifest = json.loads(Path(os.environ["STATIC_MANIFEST"]).read_text())
    print(manifest["paths"]["node/@wger-project/react-components/build/main.js"])
elif "compose" in args and "cp" in args:
    index = args.index("cp")
    source, destination = args[index + 1:index + 3]
    relative = source.removeprefix("nginx:/wger/static/")
    Path(destination).write_bytes((Path(os.environ["STATIC_ROOT"]) / relative).read_bytes())
elif "showmigrations" in args: print("[X] manager.0029")
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
                for name in ('template.html', 'history-overview.html', 'api-key.html', 'pdf.py', 'corresponding-source.json'):
                    self.assertEqual((overrides / name).read_text(), f'new {name}\n')
                commands = [json.loads(line) for line in docker_log.read_text().splitlines()]
                self.assertTrue(any(str(deploy / 'compose.yaml') in command for command in map(' '.join, commands)))
                self.assertTrue(any('cp nginx:/wger/static/' + served_name in ' '.join(command) for command in commands))
                self.assertFalse(any('build' in command for command in commands))
                self.assertTrue(all('--no-build' in command for command in commands if 'up' in command))
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
                for name in ('react-main.js', 'template.html', 'history-overview.html', 'api-key.html', 'pdf.py', 'corresponding-source.json'):
                    (candidate / 'overrides' / (name + '.next')).write_bytes((overrides / (name + '.next')).read_bytes())
                private_before = (config / 'private.env').read_bytes()
                staged_env = {**env, 'WGER_SOURCE_DEPLOY': str(candidate)}
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
                recovery = [json.loads(line) for line in docker_log.read_text().splitlines()[offset:]]
                self.assertTrue(any('pg_restore' in command for command in recovery))
                self.assertTrue(any('showmigrations' in command and any('compose.rollback.yaml' in arg for arg in command) for command in recovery))
                for service in ('web', 'celery_worker', 'celery_beat', 'powersync'):
                    self.assertTrue(any('up' in command and service in command and any('compose.rollback.yaml' in arg for arg in command) for command in recovery))
                self.assertEqual(docker_log.with_suffix('.proxy').read_text(), 'ready')
                (root / 'malformed-http').unlink()
                (deploy / 'settings-main.py').mkdir()
                staged = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env=staged_env, text=True, capture_output=True)
                self.assertEqual(staged.returncode, 0, staged.stderr)
                self.assertTrue((deploy / 'settings-main.py').is_dir())
                for name in stage_names:
                    self.assertEqual((deploy / name).read_text(), 'new ' + name)
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
                double = subprocess.run([sys.executable,str(ROOT/'release_web.py')],env={**staged_env,'RELEASE_FAULT':'double'},text=True,capture_output=True)
                self.assertNotEqual(double.returncode,0)
                self.assertIn('release migration rejected',double.stderr)
                self.assertIn('rollback recreate rejected',double.stderr)
                self.assertIn('previous compose running; public login 200',double.stderr)
                self.assertEqual(docker_log.with_suffix('.proxy').read_text(),'ready')
                storage = subprocess.run([sys.executable,str(ROOT/'release_web.py')],env={**staged_env,'RELEASE_FAULT':'double-storage'},text=True,capture_output=True)
                self.assertNotEqual(storage.returncode,0)
                self.assertIn('storage setup rejected',storage.stderr)
                self.assertNotIn('previous compose running;',storage.stderr)
                self.assertEqual(docker_log.with_suffix('.proxy').read_text(),'ready')
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
