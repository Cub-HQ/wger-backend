import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ADAPTER = Path(__file__).resolve().parents[1] / 'deploy_wger.py'


class WgerDeployTests(unittest.TestCase):
    def test_failure_receipts_keep_diagnostics_without_credentials(self):
        spec = importlib.util.spec_from_file_location('deploy_wger', ADAPTER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        errors = (
            (RuntimeError('restored application version endpoint did not become ready'), 'version endpoint'),
            (RuntimeError('release failed: mount rejected password=secret; rollback failed: recreate rejected token=abc; previous compose running'), 'rollback failed: recreate rejected'),
            (RuntimeError('rollback failed PS_DATABASE_URI=postgres://fitness_wger:secret@db/app REDIS_URL=redis://:abc@cache/0'), 'rollback failed'),
            (OSError('socket unavailable'), 'socket unavailable'),
            (ValueError('password=secret token: abc Authorization: Bearer xyz https://user:pass@host/path?key=private'), '[REDACTED]'),
            (subprocess.CalledProcessError(17, ['docker', '--password', 'hidden'], output=b'private output', stderr=b'private error'), 'exit status 17'),
            (subprocess.TimeoutExpired(['docker', '--password', 'hidden'], 30, output=b'private output'), 'timed out after 30'),
        )
        for error, diagnostic in errors:
            with self.subTest(error=type(error).__name__), patch.object(module, 'deploy', side_effect=error), patch.object(sys, 'argv', ['deploy_wger', '--source', '/unused', '--commit', 'a'*40]), patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(module.main(), 1)
                receipt=json.loads(output.getvalue())
                self.assertIn(diagnostic, receipt['reason'])
                for secret in ('secret', 'abc', 'xyz', 'user:pass', 'private', 'hidden'):
                    self.assertNotIn(secret, receipt['reason'])

    def test_unknown_surface_refuses_without_touching_live(self):
        with tempfile.TemporaryDirectory() as directory:
            live = Path(directory) / 'live'
            result = subprocess.run([sys.executable, str(ADAPTER), '--source', directory,
                                     '--commit', 'a' * 40, '--deploy-root', str(live),
                                     '--changed-file', 'deployment/wger/config/unknown.conf'],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout.splitlines()[-1])['status'], 'DEPLOY_MISSING')
            self.assertFalse(live.exists())

    def test_legacy_patches_without_release_consumers_refuse(self):
        spec = importlib.util.spec_from_file_location('deploy_wger', ADAPTER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in ('patch_server_wave3.py', 'patch_ux_wave1.py', 'patch_ux_wave3.py'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'DEPLOY_MISSING'):
                module.check_surfaces(['deployment/wger/patches/' + name])

    def test_bundle_comparison_normalizes_only_collectstatic_map(self):
        spec = importlib.util.spec_from_file_location('deploy_wger', ADAPTER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        expected = b'feature();\n//# sourceMappingURL=main.js.map'
        actual = expected.replace(b'main.js.map', b'main.js.123456789abc.map')
        self.assertEqual(module.normalize_bundle(actual), expected)
        self.assertNotEqual(module.normalize_bundle(b'stale();' + actual), expected)


if __name__ == '__main__':
    unittest.main()
