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

    def test_non_release_surfaces_refuse_with_explicit_reason(self):
        spec = importlib.util.spec_from_file_location('deploy_wger', ADAPTER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cases = {
            'operations/recovery-drill.py': 'operations activation',
            'patches/patch_server_wave3.py': 'retired release input',
            'config/unknown.conf': 'unsupported surface',
        }
        for name, reason in cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, reason):
                module.check_surfaces(['deployment/wger/' + name])

    def test_every_preparer_dependency_is_a_product_input(self):
        spec = importlib.util.spec_from_file_location('deploy_wger', ADAPTER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = Path(__file__).resolve().parents[2] / 'deployment/wger'
        preparer = (source / 'patches/prepare-react.sh').read_text()
        referenced = {
            'patches/' + name
            for name in module.re.findall(r'\$PATCH_DIR/([A-Za-z0-9_.-]+\.py)', preparer)
        }
        self.assertTrue(referenced)
        self.assertTrue(referenced.issubset(module.PRODUCT_FILES))
        self.assertIn('formats/en_AU/formats.py', module.PRODUCT_FILES)
        module.check_surfaces(['deployment/wger/' + name for name in referenced | {'formats/en_AU/formats.py'}])

    def test_every_repository_gym_file_has_an_explicit_surface_class(self):
        spec = importlib.util.spec_from_file_location('deploy_wger', ADAPTER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        root = Path(__file__).resolve().parents[2] / 'deployment/wger'
        actual = {path.relative_to(root).as_posix() for path in root.rglob('*')
                  if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'}
        classes = {
            'product': set(module.PRODUCT_FILES),
            'machinery': set(module.MACHINERY),
            'preflight': set(module.PREFLIGHT_FILES),
            'operations': set(module.OPERATIONS),
            'evidence': set(module.EVIDENCE_FILES),
            'retired': set(module.RETIRED_FILES),
        }
        counts = {}
        for kind, paths in classes.items():
            for path in paths:
                self.assertIn(path, actual, f'{kind} surface no longer exists: {path}')
                counts[path] = counts.get(path, 0) + 1
        self.assertEqual(actual, set(counts), 'every gym file must have an explicit surface class')
        self.assertFalse({path for path, count in counts.items() if count != 1},
                         'gym files must belong to exactly one surface class')
        module.check_surfaces(['deployment/wger/' + name for name in
                               classes['product'] | classes['machinery'] |
                               classes['preflight'] | classes['evidence']])

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
