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
        self.assertIn('a3f3b9d407f3f799f87d8600c73394b34a28db33', script)
        self.assertIn('Cubatica/wger', script)
        self.assertIn('react-main.js.next', script)
        self.assertIn('template.html.next', script)
        self.assertNotIn('compose up', script)
    def test_release_refuses_without_both_writer_locks_and_recreates_only_web(self):
        wrapper = (ROOT / 'release-web.sh').read_text()
        script = (ROOT / 'release_web.py').read_text()
        self.assertIn('release_web.py', wrapper)
        self.assertIn('WGER_WRITER_LOCK', script)
        self.assertIn('WGER_HISTORY_LOCK', script)
        self.assertIn('fcntl.LOCK_EX | fcntl.LOCK_NB', script)
        self.assertIn("(Path(history_lock), 'lockf')", script)
        self.assertIn("(Path(writer_lock), 'flock')", script)
        self.assertIn("'--force-recreate', 'web'", script)
        self.assertNotIn("'--force-recreate', 'db'", script)
        self.assertIn('previous overrides restored', script)

    def test_footer_advertises_public_corresponding_source(self):
        patcher = (ROOT / 'patch_footer.py').read_text()
        self.assertIn('github.com/Cubatica/wger/tree/65a1d405', patcher)
        self.assertIn('github.com/Cubatica/react/tree/a3f3b9d', patcher)

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
