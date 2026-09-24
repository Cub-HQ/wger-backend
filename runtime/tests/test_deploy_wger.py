import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ADAPTER = Path(__file__).resolve().parents[1] / 'deploy_wger.py'


class WgerDeployTests(unittest.TestCase):
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
