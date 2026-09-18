import fcntl
import os
import pathlib
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
