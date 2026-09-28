import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
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
            'operations/cleanup-recovery.py': 'operations activation',
            'operations/com.cortana.fitness-wger.backup.plist': 'operations activation',
            'com.cortana.fitness-wger.vm.plist': 'operations activation',
            'patches/patch_server_wave3.py': 'retired release input',
            'config/unknown.conf': 'unsupported surface',
        }
        for name, reason in cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, reason):
                module.check_surfaces(['deployment/wger/' + name])

    def test_layout_cutover_accepts_only_absent_retired_inputs(self):
        spec = importlib.util.spec_from_file_location('deploy_wger', ADAPTER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        changed = [module.PREFIX + name for name in
                   ('Dockerfile', 'settings-main.py', 'compose.yaml',
                    'overrides/settings-main.py', 'overrides/manager-urls.py',
                    'formats/en_AU/formats.py', 'patches/prepare-react.sh',
                    'patches/release_web.py', 'patches/test_release_route.py',
                    'patches/check_pinned_artifacts.py',
                    'operations/backup.py', 'operations/snapshot.py', 'operations/restore-drill.py',
                    'operations/recovery-drill.py', 'operations/test_backup_route.py')]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            root = source / module.PREFIX
            root.mkdir(parents=True)
            module.check_surfaces(changed, source)
            for name in ('Dockerfile', 'settings-main.py'):
                retired = root / name
                for kind in ('file', 'directory', 'symlink'):
                    with self.subTest(name=name, kind=kind):
                        if kind == 'file':
                            retired.write_text('retired input')
                        elif kind == 'directory':
                            retired.mkdir()
                        else:
                            retired.symlink_to(root / 'missing-target')
                        with self.assertRaisesRegex(ValueError, 'unsupported surface'):
                            module.check_surfaces(changed, source)
                        if kind == 'directory':
                            retired.rmdir()
                        else:
                            retired.unlink()

    def test_stock_product_stages_bind_sources_without_image_build(self):
        spec = importlib.util.spec_from_file_location('deploy_wger', ADAPTER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            home.mkdir()
            live = Path(directory) / 'live'
            (live / 'config').mkdir(parents=True)
            (live / 'config/private.env').write_text('private fixture')

            def prepare(args, **kwargs):
                self.assertEqual(args[0], 'bash', 'stock image must not be rebuilt')
                script = Path(args[1]).resolve()
                self.assertEqual(script.name, 'prepare-react.sh')
                self.assertTrue(script.is_relative_to(home.resolve()))
                candidate = script.parents[1]
                self.assertTrue(candidate.is_relative_to((home / '.cache').resolve()))
                for name in ('overrides/settings-main.py', 'overrides/manager-urls.py',
                             'formats/en_AU/formats.py', 'patches/patch_session_recovery.py',
                             'patches/patch_session_recovery_ui.py',
                             'patches/test_patch_session_recovery_ui.py',
                             'patches/test_patch_session_recovery.py'):
                    self.assertEqual((candidate / name).read_bytes(),
                                     (source / module.PREFIX / name).read_bytes())
                self.assertFalse((candidate / 'Dockerfile').exists())
                self.assertFalse((candidate / 'operations/recovery-drill.py').exists())
                raise RuntimeError('stop before backup or live release')

            args = SimpleNamespace(source=str(source), commit='a' * 40,
                                   deploy_root=str(live),
                                   changed_file=['deployment/wger/operations/recovery-drill.py'])
            with patch.object(module.Path, 'home', return_value=home), \
                    patch.object(module.subprocess, 'check_output', side_effect=['a' * 40, '']), \
                    patch.object(module, 'existing_locks', return_value={}), \
                    patch.object(module.subprocess, 'run', side_effect=prepare):
                with self.assertRaisesRegex(RuntimeError, 'stop before backup or live release'):
                    module.deploy(args)

    def test_escaped_cache_refuses_before_preparation(self):
        spec = importlib.util.spec_from_file_location('deploy_wger', ADAPTER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            home.mkdir()
            outside = Path(directory) / 'outside'
            outside.mkdir()
            try:
                (home / '.cache').symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f'directory symlinks unavailable: {error}')
            self.assertFalse((home / '.cache').resolve().is_relative_to(home.resolve()))
            live = Path(directory) / 'live'
            (live / 'config').mkdir(parents=True)
            (live / 'config/private.env').write_text('private fixture')
            args = SimpleNamespace(source=str(source), commit='a' * 40,
                                   deploy_root=str(live), changed_file=[])
            with patch.object(module.Path, 'home', return_value=home), \
                    patch.object(module.subprocess, 'check_output', side_effect=['a' * 40, '']), \
                    patch.object(module, 'existing_locks', return_value={}), \
                    patch.object(module.subprocess, 'run') as prepare:
                with self.assertRaises(ValueError):
                    module.deploy(args)
                prepare.assert_not_called()

    def test_unsafe_cache_permissions_refuse_before_preparation(self):
        spec = importlib.util.spec_from_file_location('deploy_wger', ADAPTER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            home.mkdir(mode=0o700)
            cache = home / '.cache'
            cache.mkdir(mode=0o700)
            live = Path(directory) / 'live'
            (live / 'config').mkdir(parents=True)
            (live / 'config/private.env').write_text('private fixture')
            args = SimpleNamespace(source=str(source), commit='a' * 40,
                                   deploy_root=str(live), changed_file=[])
            try:
                for mode in (0o770, 0o707):
                    with self.subTest(mode=oct(mode)):
                        cache.chmod(mode)
                        with patch.object(module.Path, 'home', return_value=home), \
                                patch.object(module.subprocess, 'check_output', side_effect=['a' * 40, '']), \
                                patch.object(module, 'existing_locks', return_value={}), \
                                patch.object(module.subprocess, 'run') as prepare:
                            with self.assertRaisesRegex(ValueError, 'DEPLOY_MISSING: staging cache'):
                                module.deploy(args)
                            prepare.assert_not_called()
            finally:
                cache.chmod(0o700)

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
            'source-only': set(module.SOURCE_ONLY_FILES),
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
                               classes['preflight'] | classes['source-only'] | classes['evidence']])

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
