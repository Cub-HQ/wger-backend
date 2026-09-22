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

ROOT = pathlib.Path(__file__).parent


class ReleaseRouteTest(unittest.TestCase):
    def test_builder_is_colima_local_and_stages_only(self):
        script = (ROOT / 'prepare-react.sh').read_text()
        self.assertNotIn('limactl', script)
        self.assertIn('.colima/default/docker.sock', script)
        self.assertNotIn('docker context inspect', script)
        self.assertIn('3066f7693ac00632ad14ea0ef025371156f91d0d', script)
        self.assertIn('135d8569a3eb27c9f0f74e865d56372421a61294', script)
        self.assertIn('react-main.js.next', script)
        self.assertIn('template.html.next', script)
        self.assertNotIn('compose up', script)
    def test_release_refuses_without_both_writer_locks_and_restores_exact_runtime(self):
        wrapper = (ROOT / 'release-web.sh').read_text()
        script = (ROOT / 'release_web.py').read_text()
        self.assertIn('release_web.py', wrapper)
        self.assertNotIn('source ', wrapper)
        self.assertNotIn('private.env', wrapper)
        self.assertIn('read_database_environment(private_env)', script)
        self.assertNotIn('POSTGRES_PASSWORD', script)
        self.assertIn('WGER_WRITER_LOCK', script)
        self.assertIn('WGER_HISTORY_LOCK', script)
        self.assertIn('fcntl.LOCK_EX | fcntl.LOCK_NB', script)
        self.assertIn("(Path(history_lock), 'lockf')", script)
        self.assertIn("(Path(writer_lock), 'flock')", script)
        self.assertIn("compose('stop', 'powersync', *services)", script)
        self.assertLess(script.index("compose('stop', 'powersync', *services)"), script.index("'pg_dump', '-Fc'"))
        self.assertIn("'dropdb', '--if-exists', '--force'", script)
        self.assertIn("rollback_images = {**prior_images, 'powersync': prior_powersync_image}", script)
        self.assertIn('resumed_image != prior_powersync_image or resumed_state != prior_powersync_state', script)
        self.assertIn("'pg_restore', '--exit-on-error'", script)
        self.assertIn('snapshot_complete and restored_schema != prior_schema', script)
        self.assertNotIn("'--force-recreate', 'db'", script)
        self.assertIn('prior writer states, overrides, database schema/data and exact images restored', script)
    def test_release_targets_selected_live_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            home = root / 'home'
            socket_path = home / '.colima/default/docker.sock'
            socket_path.parent.mkdir(parents=True)
            docker_socket = socket.socket(socket.AF_UNIX)
            docker_socket.bind(str(socket_path))
            try:
                deploy = root / 'live'
                overrides = deploy / 'overrides'
                config = deploy / 'config'
                overrides.mkdir(parents=True)
                config.mkdir()
                (deploy / 'compose.yaml').write_text('services: {}\n')
                (config / 'private.env').write_text('POSTGRES_USER=fitness_wger\nPOSTGRES_DB=fitness_wger\n')
                for name in ('react-main.js', 'template.html', 'corresponding-source.json'):
                    (overrides / f'{name}.next').write_text(f'new {name}\n')
                writer, history = root / 'writer.lock', root / 'history.lock'
                writer.touch(); history.touch()
                docker_log = root / 'docker.jsonl'
                binary = root / 'bin/docker'
                binary.parent.mkdir()
                binary.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["DOCKER_LOG"]).open("a") as log: log.write(json.dumps(args) + "\\n")
if "compose" in args and "ps" in args and "-q" in args:
    services = args[args.index("-q") + 1:]
    print("\\n".join({"web":"id-web", "celery_worker":"id-worker", "celery_beat":"id-beat", "powersync":"id-powersync"}[service] for service in services))
elif "inspect" in args:
    template, target = args[args.index("-f") + 1], args[-1]
    if template == "{{.Image}}": print("image-" + target)
    elif template == "{{.State.Status}}": print("running")
    else: print("healthy")
elif "showmigrations" in args: print("[X] manager.0029")
elif "pg_dump" in args: sys.stdout.buffer.write(b"database")
''')
                binary.chmod(0o755)
                env = {**os.environ, 'HOME': str(home), 'PATH': str(binary.parent) + ':' + os.environ['PATH'],
                       'DOCKER_LOG': str(docker_log), 'WGER_DEPLOY_DIR': str(deploy),
                       'WGER_WRITER_LOCK': str(writer), 'WGER_HISTORY_LOCK': str(history)}
                result = subprocess.run([sys.executable, str(ROOT / 'release_web.py')], env=env,
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                for name in ('react-main.js', 'template.html', 'corresponding-source.json'):
                    self.assertEqual((overrides / name).read_text(), f'new {name}\n')
                commands = [json.loads(line) for line in docker_log.read_text().splitlines()]
                self.assertTrue(any(str(deploy / 'compose.yaml') in command for command in map(' '.join, commands)))
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

    def test_fork_image_is_used_by_live_and_restore_routes(self):
        commit = '135d8569a3eb27c9f0f74e865d56372421a61294'
        deployment = ROOT.parent
        self.assertIn(commit, (deployment / 'Dockerfile').read_text())
        self.assertIn(commit, (deployment / 'compose.yaml').read_text())
        self.assertIn(commit, (deployment / 'operations/restore-drill.py').read_text())
        self.assertIn(commit, (deployment / 'operations/recovery-drill.py').read_text())
        self.assertIn("DJANGO_PERFORM_MIGRATIONS='True'", (deployment / 'operations/restore-drill.py').read_text())

    def test_footer_advertises_public_corresponding_source(self):
        patcher = (ROOT / 'patch_footer.py').read_text()
        self.assertIn('github.com/Cubatica/wger/tree/135d8569', patcher)
        self.assertIn('github.com/Cubatica/react/tree/3066f769', patcher)

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
