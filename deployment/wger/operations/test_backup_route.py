import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import runpy
import socket
import sqlite3
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

import backup
from snapshot import REQUIRED, RESTORE_ARTIFACTS
RESTORE_MODULE = pathlib.Path(__file__).with_name('restore-drill.py')
RESTORE_SPEC = importlib.util.spec_from_file_location('restore_drill', RESTORE_MODULE)
restore_drill = importlib.util.module_from_spec(RESTORE_SPEC)
RESTORE_SPEC.loader.exec_module(restore_drill)
PREFLIGHT_SPEC = importlib.util.spec_from_file_location(
    'wger_preflight', pathlib.Path(__file__).resolve().parents[3] / 'runtime/wger_preflight.py')
preflight = importlib.util.module_from_spec(PREFLIGHT_SPEC)
PREFLIGHT_SPEC.loader.exec_module(preflight)

CURRENT_MOUNTS = (
    ('overrides/react-main.js', '/home/wger/src/node_modules/@wger-project/react-components/build/main.js'),
    ('overrides/template.html', '/home/wger/src/wger/core/templates/template.html'),
    ('overrides/settings-main.py', '/home/wger/src/settings/main.py'),
    ('overrides/manager-urls.py', '/home/wger/src/wger/manager/urls.py'),
)
REVIEWED_MOUNTS = CURRENT_MOUNTS + (
    ('overrides/history-overview.html', '/home/wger/src/wger/exercises/templates/history/overview.html'),
    ('overrides/api-key.html', '/home/wger/src/wger/core/templates/user/api_key.html'),
    ('overrides/pdf.py', '/home/wger/src/wger/utils/pdf.py'),
    ('formats/en_AU/formats.py', '/home/wger/src/wger/formats/en_AU/formats.py'),
    ('overrides/manager-session-recovery.py', '/home/wger/src/wger/manager/models/session_recovery.py'),
    ('overrides/manager-models-init.py', '/home/wger/src/wger/manager/models/__init__.py'),
    ('overrides/manager-api-views.py', '/home/wger/src/wger/manager/api/views.py'),
    ('overrides/manager-tasks.py', '/home/wger/src/wger/manager/tasks.py'),
    ('overrides/manager-log.py', '/home/wger/src/wger/manager/models/log.py'),
    ('overrides/manager-0030-workoutlog-cardio-metrics.py', '/home/wger/src/wger/manager/migrations/0030_workoutlog_cardio_metrics.py'),
    ('overrides/manager-0031-session-recovery.py', '/home/wger/src/wger/manager/migrations/0031_workoutsessionrecovery.py'),
)


def services_for(root, mounts=REVIEWED_MOUNTS):
    return {'web': {'image': 'docker.io/wger/server:2.7@sha256:' + '1' * 64,
                    'volumes': [{'type': 'bind', 'source': str(root / source), 'target': target, 'read_only': True}
                                for source, target in mounts] +
                               [{'type': 'volume', 'source': 'media', 'target': '/home/wger/media'}]}}



class BackupRouteTest(unittest.TestCase):
    def test_compose_layout_requires_all_declared_binds_and_ignores_unbound_files(self):
        for mounts in (CURRENT_MOUNTS, REVIEWED_MOUNTS):
            with self.subTest(mounts=mounts), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                (root / 'compose.yaml').write_text('services: {}')
                (root / 'config').mkdir()
                (root / 'config/private.env').write_text('PRIVATE=yes')
                for source, _ in mounts:
                    path = root / source
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(source)
                (root / 'overrides/unbound.py').write_text('not mounted')
                services = services_for(root, mounts)
                result = type('Result', (), {'stdout': json.dumps({'services': services}).encode()})()
                with patch.object(backup.subprocess, 'run', return_value=result), patch.object(backup.pathlib.Path, 'is_socket', return_value=True):
                    resolved = backup.deployment_services(root)
                self.assertEqual(restore_drill.web_override_mounts(root, resolved), tuple(
                    f'{root / source}:{target}:ro' for source, target in mounts))
                (root / mounts[-1][0]).unlink()
                with self.assertRaisesRegex(RuntimeError, 'required deployment bind source is missing'):
                    restore_drill.web_override_mounts(root, resolved)

    def test_compose_bind_sources_cannot_escape_or_follow_parent_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            work = root / 'work'; work.mkdir()
            outside = root / 'outside'; outside.mkdir()
            (outside / 'override.py').write_text('external')
            (work / 'linked').symlink_to(outside, target_is_directory=True)
            for source in (str(outside / 'override.py'), '../outside/override.py', 'linked/override.py'):
                service = {'volumes': [{'type': 'bind', 'source': source, 'target': '/override.py', 'read_only': True}]}
                with self.subTest(source=source), self.assertRaises(RuntimeError):
                    backup.service_bind_mounts(work, service)

    def test_optional_archive_member_rejects_parent_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            deploy = root / 'deploy'; deploy.mkdir()
            outside = root / 'outside'; (outside / 'en_AU').mkdir(parents=True)
            (outside / 'en_AU/formats.py').write_text('outside')
            (deploy / 'formats').symlink_to(outside, target_is_directory=True)
            archive = root / 'deployment.tar'
            with self.assertRaises(RuntimeError):
                backup.write_deployment_archive(archive, deploy=deploy)
            self.assertFalse(archive.exists())

    def test_snapshot_recovers_proxy_after_writer_restart_even_on_capture_failure(self):
        for fault in (None, 'capture', 'reload', 'powersync', 'powersync+reload'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                home=pathlib.Path(directory); destination=home/'fitness-coach-migration'
                destination.mkdir(mode=0o700); destination.chmod(0o700)
                (home/'compose.yaml').touch()
                actions=[]; proxy={'address':'old', 'web':'old'}
                states={service:{'image':service,'status':'running','health':'healthy'} for service in backup.WRITERS}
                def compose(*args,**kwargs):
                    actions.append(args)
                    if args[:3]==('ps','-a','-q'):return args[3].encode()
                    if args[:3]==('exec','-T','db'):
                        if fault=='capture':raise RuntimeError('capture failed')
                        return b''
                    if args==('exec','-T','nginx','nginx','-t'):return b''
                    if args==('exec','-T','nginx','nginx','-s','reload'):
                        if fault in ('reload','powersync+reload'):raise RuntimeError('reload failed')
                        proxy['address']=proxy['web'];return b''
                    raise AssertionError(args)
                def docker(*args,**kwargs):
                    actions.append(args)
                    if args[0]=='start':proxy['web']='new'
                    return b''
                def wait_for_state(ident, expected):
                    if fault in ('powersync','powersync+reload') and ident=='powersync':raise RuntimeError('powersync failed')
                    self.assertEqual(states[ident],expected)
                with patch.object(backup.pathlib.Path,'home',return_value=home), patch.object(backup,'DEPLOY',home), patch.object(backup.pathlib.Path,'is_socket',return_value=True), patch.object(backup,'acquire_custody',return_value=[]), patch.object(backup,'compose',side_effect=compose), patch.object(backup,'docker',side_effect=docker), patch.object(backup,'inspect_state',side_effect=lambda ident:states[ident]), patch.object(backup,'wait_for_state',side_effect=wait_for_state), patch.object(backup,'media_inventory',return_value=b''), patch.object(backup.subprocess,'run'), patch.object(backup,'write_deployment_archive'), patch.object(backup,'write_manifest'):
                    if fault:
                        with self.assertRaisesRegex(RuntimeError,fault.split('+')[0]+' failed') as caught:backup.snapshot(destination)
                        if fault=='powersync+reload':self.assertIn('nginx recovery also failed: reload failed',caught.exception.__notes__)
                    else:backup.snapshot(destination)
                if fault not in ('reload','powersync+reload'):self.assertEqual(proxy['address'],proxy['web'])
                self.assertLess(next(i for i,a in enumerate(actions) if a[0]=='start'),actions.index(('exec','-T','nginx','nginx','-s','reload')))
                self.assertEqual((destination/'latest.json').exists(),fault is None)
                self.assertEqual((next(destination.glob('wger-*'))/'INCOMPLETE').exists(),fault is not None)

    def test_writer_inspection_failure_prevents_capture_and_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            home=pathlib.Path(directory);destination=home/'fitness-coach-migration';destination.mkdir(mode=0o700);destination.chmod(0o700)
            native=home/'native.lock';state=home/'state.lock';native.touch();state.touch()
            ids={service:f'id-{service}' for service in backup.WRITERS};actions=[]
            def compose(*args,**kwargs):
                if args[:3]==('ps','-a','-q'):return ids[args[3]].encode()
                raise AssertionError(args)
            def docker(*args,**kwargs):
                actions.append(args)
                if args[:2]==('inspect','id-powersync'):raise RuntimeError('inspect transport failure')
                raise AssertionError(args)
            with patch.object(backup.pathlib.Path,'home',return_value=home),patch.object(backup,'DEPLOY',home),patch.dict(os.environ,{'WGER_DOCKER_HOST':'unix:///tmp/proof.sock'}),patch.object(backup.pathlib.Path,'is_socket',return_value=True),patch.object(backup,'compose',side_effect=compose),patch.object(backup,'docker',side_effect=docker),patch.dict(os.environ,{'WGER_WRITER_LOCK':str(native),'WGER_HISTORY_LOCK':str(state)}):
                (home/'compose.yaml').touch()
                with self.assertRaisesRegex(RuntimeError,'inspect transport failure'):backup.snapshot(destination)
            self.assertFalse(any(action[0] in {'stop','run'} for action in actions))
            snapshot=next(destination.glob('wger-*'));self.assertTrue((snapshot/'INCOMPLETE').exists());self.assertFalse((destination/'latest.json').exists())

    def test_structured_inspection_distinguishes_absent_health_from_error(self):
        payload=b'[{"Image":"sha256:writer","State":{"Status":"running"}}]'
        with patch.object(backup,'docker',return_value=payload) as docker:
            self.assertEqual(backup.inspect_state('writer'),{'image':'sha256:writer','status':'running','health':None})
        docker.assert_called_once_with('inspect','writer')

    def test_capture_failure_marks_incomplete_restores_same_ids_and_never_publishes_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            home=pathlib.Path(directory);destination=home/'fitness-coach-migration';destination.mkdir(mode=0o700);destination.chmod(0o700)
            native=home/'native.lock';state=home/'state.lock';native.touch();state.touch()
            ids={service:f'id-{service}' for service in backup.WRITERS};states={service:{'image':f'image-{service}','status':'running','health':'healthy'} for service in backup.WRITERS}
            def compose(*args,**kwargs):
                if args[:3]==('ps','-a','-q'):return ids[args[3]].encode()
                if args[:3]==('exec','-T','db'):raise RuntimeError('forced capture failure')
                if args[:4]==('exec','-T','nginx','nginx'):return b''
                raise AssertionError(args)
            starts=[]
            def docker(*args,**kwargs):
                if args[0]=='stop':return b''
                if args[0]=='start':starts.extend(args[1:]);return b''
                raise AssertionError(args)
            with patch.object(backup.pathlib.Path,'home',return_value=home),patch.object(backup,'DEPLOY',home),patch.dict(os.environ,{'WGER_DOCKER_HOST':'unix:///tmp/proof.sock'}),patch.object(backup.pathlib.Path,'is_socket',return_value=True),patch.object(backup,'compose',side_effect=compose),patch.object(backup,'docker',side_effect=docker),patch.object(backup,'inspect_state',side_effect=lambda ident:states[ident.removeprefix('id-')]),patch.object(backup,'media_inventory',return_value=b''),patch.dict(os.environ,{'WGER_WRITER_LOCK':str(native),'WGER_HISTORY_LOCK':str(state)}):
                (home/'compose.yaml').touch()
                with self.assertRaisesRegex(RuntimeError,'forced capture failure') as caught:backup.snapshot(destination)
            self.assertFalse(getattr(caught.exception,'__notes__',[]))
            self.assertEqual(set(starts),set(ids.values()))
            snapshots=list(destination.glob('wger-*'));self.assertEqual(len(snapshots),1)
            self.assertTrue((snapshots[0]/'INCOMPLETE').exists());self.assertFalse((destination/'latest.json').exists())

    def test_capture_and_resume_failure_preserves_primary_and_marks_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            home=pathlib.Path(directory);destination=home/'fitness-coach-migration';destination.mkdir(mode=0o700);destination.chmod(0o700)
            native=home/'native.lock';state=home/'state.lock';native.touch();state.touch()
            ids={service:f'id-{service}' for service in backup.WRITERS};states={service:{'image':f'image-{service}','status':'running','health':'healthy'} for service in backup.WRITERS}
            def compose(*args,**kwargs):
                if args[:3]==('ps','-a','-q'):return ids[args[3]].encode()
                if args[:3]==('exec','-T','db'):raise RuntimeError('primary capture failure')
                if args[:4]==('exec','-T','nginx','nginx'):return b''
                raise AssertionError(args)
            def docker(*args,**kwargs):
                if args[0] in {'stop','start'}:return b''
                raise AssertionError(args)
            def wait_for_state(*_):raise RuntimeError('resume health failure')
            with patch.object(backup.pathlib.Path,'home',return_value=home),patch.object(backup,'DEPLOY',home),patch.dict(os.environ,{'WGER_DOCKER_HOST':'unix:///tmp/proof.sock'}),patch.object(backup.pathlib.Path,'is_socket',return_value=True),patch.object(backup,'compose',side_effect=compose),patch.object(backup,'docker',side_effect=docker),patch.object(backup,'inspect_state',side_effect=lambda ident:states[ident.removeprefix('id-')]),patch.object(backup,'wait_for_state',side_effect=wait_for_state),patch.object(backup,'media_inventory',return_value=b''),patch.dict(os.environ,{'WGER_WRITER_LOCK':str(native),'WGER_HISTORY_LOCK':str(state)}):
                (home/'compose.yaml').touch()
                with self.assertRaisesRegex(RuntimeError,'primary capture failure') as caught:backup.snapshot(destination)
            self.assertTrue(any('writer recovery also failed: resume health failure' in note for note in getattr(caught.exception,'__notes__',[])))
            snapshot=next(destination.glob('wger-*'));self.assertTrue((snapshot/'INCOMPLETE').exists());self.assertFalse((destination/'latest.json').exists())

    def test_symlinked_approved_destination_writes_nothing_to_redirect_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);home=root/'home';redirect=root/'redirect';home.mkdir(mode=0o700);redirect.mkdir(mode=0o700)
            destination=home/'fitness-coach-migration';destination.symlink_to(redirect,target_is_directory=True)
            with patch.object(backup.pathlib.Path,'home',return_value=home):
                with self.assertRaisesRegex(ValueError,'private real directory'):backup.snapshot(destination)
            self.assertEqual(list(redirect.iterdir()),[])

    def test_symlinked_home_ancestor_writes_nothing_to_redirect_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);actual=root/'actual';actual.mkdir(mode=0o700)
            link=root/'link';link.symlink_to(actual,target_is_directory=True)
            home=link/'home';(actual/'home').mkdir(mode=0o700)
            destination=home/'fitness-coach-migration'
            with patch.object(backup.pathlib.Path,'home',return_value=home):
                with self.assertRaisesRegex(ValueError,'ancestor must be a real directory'):backup.snapshot(destination)
            self.assertEqual(list((actual/'home').iterdir()),[])

    def test_existing_destination_requires_owner_only_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            home=pathlib.Path(directory);destination=home/'fitness-coach-migration';destination.mkdir();destination.chmod(0o755)
            with patch.object(backup.pathlib.Path,'home',return_value=home):
                with self.assertRaisesRegex(ValueError,'owner-owned private real directory'):backup.snapshot(destination)
            self.assertEqual(list(destination.iterdir()),[])

    def test_destination_is_established_private_directory_only(self):
        with self.assertRaises(ValueError):backup.snapshot('/tmp/not-approved')
    def test_archive_default_tracks_current_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name in ('first', 'second'):
                deploy = root / name
                (deploy / 'config').mkdir(parents=True)
                (deploy / 'overrides').mkdir()
                (deploy / 'compose.yaml').write_text(name)
                (deploy / 'settings-main.py').write_text("LANGUAGE_CODE = 'en-au'\n")
                archive = root / (name + '.tar')
                with patch.object(backup, 'DEPLOY', deploy):
                    backup.write_deployment_archive(archive)
                with tarfile.open(archive) as captured:
                    self.assertEqual(captured.extractfile('compose.yaml').read(), name.encode())
                    self.assertEqual(captured.extractfile('settings-main.py').read(), b"LANGUAGE_CODE = 'en-au'\n")

    def test_restore_requires_existing_browser_template_and_settings_overrides(self):
        for missing, _ in REVIEWED_MOUNTS:
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                for source, _ in REVIEWED_MOUNTS:
                    if source != missing:
                        path = root / source
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text('reviewed file')
                with self.assertRaises(RuntimeError):
                    restore_drill.web_override_mounts(root, services_for(root))

    def test_restore_selects_postgresql_after_removing_powersync_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            deploy = root / 'deploy'
            (deploy / 'config').mkdir(parents=True)
            (deploy / 'overrides').mkdir()
            (deploy / 'compose.yaml').write_text('services: {}')
            (deploy / 'config/private.env').write_text('PS_DATABASE_URI=postgres://private\n')
            snapshot = root / 'snapshot'
            snapshot.mkdir()
            backup.write_deployment_archive(snapshot / 'deployment.tar', deploy=deploy)
            (snapshot / 'manifest.json').write_text('{"files": {}}')
            with patch('sys.argv', ['restore-drill.py', str(snapshot)]), patch.object(backup.pathlib.Path, 'is_socket', return_value=True), patch.object(restore_drill, 'deployment_services', return_value=services_for(deploy, ())), patch.object(restore_drill, 'run', side_effect=RuntimeError('stop before Docker')):
                with self.assertRaisesRegex(RuntimeError, 'stop before Docker'):
                    restore_drill.main()
            environment = dict(line.split('=', 1) for line in next(root.glob('wger-restore-*/restore.env')).read_text().splitlines())
            self.assertEqual(environment['DJANGO_DB_ENGINE'], 'django.db.backends.postgresql')
            self.assertNotIn('PS_DATABASE_URI', environment)
            self.assertEqual(environment['DJANGO_DB_USER'], 'restore')

    def test_pre_release_snapshot_and_restore_skip_not_yet_installed_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            deploy = root / 'deploy'
            (deploy / 'config').mkdir(parents=True)
            (deploy / 'overrides').mkdir()
            (deploy / 'compose.yaml').write_text('services: {}\n')
            (deploy / 'config' / 'private.env').write_text('PRIVATE=yes\n')
            for source, _ in CURRENT_MOUNTS:
                (deploy / source).write_text(source)
            archive = root / 'deployment.tar'

            backup.write_deployment_archive(archive, deploy=deploy)

            restored = root / 'restored'
            restored.mkdir()
            with tarfile.open(archive) as captured:
                captured.extractall(restored, filter='data')
                names = set(captured.getnames())
            self.assertTrue({'compose.yaml', 'config/private.env', *(source for source, _ in CURRENT_MOUNTS)} <= names)
            self.assertTrue({source for source, _ in REVIEWED_MOUNTS
                             if source not in dict(CURRENT_MOUNTS)}.isdisjoint(names))
            expected = tuple(str(restored / source) + ':' + target + ':ro' for source, target in CURRENT_MOUNTS)
            self.assertEqual(restore_drill.web_override_mounts(restored, services_for(restored, CURRENT_MOUNTS)), expected)

    def test_snapshot_and_restore_keep_all_reviewed_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            deploy = root / 'deploy'
            (deploy / 'config').mkdir(parents=True)
            (deploy / 'compose.yaml').write_text('services: {}\n')
            (deploy / 'config' / 'private.env').write_text('PRIVATE=yes\n')
            contents = {source: source + '\n' for source, _ in REVIEWED_MOUNTS}
            contents['overrides/settings-main.py'] = "LANGUAGE_CODE = 'en-au'\n"
            contents['formats/en_AU/formats.py'] = "DATE_FORMAT = 'd/m/Y'\n"
            for source, content in contents.items():
                path = deploy / source
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            (deploy / 'overrides' / 'unreviewed.py').write_text('not a reviewed mount\n')
            archive = root / 'deployment.tar'

            backup.write_deployment_archive(archive, deploy=deploy)

            restored = root / 'restored'
            restored.mkdir()
            with tarfile.open(archive) as captured:
                captured.extractall(restored, filter='data')
                self.assertTrue(set(contents) <= set(captured.getnames()))
            for source, content in contents.items():
                self.assertEqual((restored / source).read_text(), content)
            self.assertTrue((restored / 'overrides' / 'unreviewed.py').is_file())
            self.assertEqual(restore_drill.web_override_mounts(restored, services_for(restored)), tuple(
                str(restored / source) + ':' + target + ':ro'
                for source, target in REVIEWED_MOUNTS
            ))

    def test_optional_archive_members_reject_wrong_types(self):
        for source in ('settings-main.py', 'formats/en_AU/formats.py'):
            for kind in ('directory', 'symlink', 'dangling-symlink'):
                with self.subTest(source=source, kind=kind), tempfile.TemporaryDirectory() as directory:
                    root = pathlib.Path(directory)
                    deploy = root / 'deploy'
                    (deploy / 'config').mkdir(parents=True)
                    (deploy / 'overrides').mkdir()
                    (deploy / 'compose.yaml').write_text('services: {}\n')
                    path = deploy / source
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if kind == 'directory':
                        path.mkdir()
                    else:
                        target = root / 'target'
                        if kind == 'symlink':
                            target.write_text('regular file\n')
                        path.symlink_to(target)
                    archive = root / 'deployment.tar'
                    with self.assertRaises(RuntimeError):
                        backup.write_deployment_archive(archive, deploy=deploy)
                    self.assertFalse(archive.exists())

    def test_reviewed_restore_mounts_reject_wrong_types(self):
        for source, _ in REVIEWED_MOUNTS:
            for kind in ('directory', 'symlink', 'dangling-symlink'):
                with self.subTest(source=source, kind=kind), tempfile.TemporaryDirectory() as directory:
                    root = pathlib.Path(directory)
                    work = root / 'restored'
                    for required, _ in REVIEWED_MOUNTS:
                        if required != source:
                            member = work / required
                            member.parent.mkdir(parents=True, exist_ok=True)
                            member.write_text('reviewed override')
                    path = work / source
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if kind == 'directory':
                        path.mkdir()
                    else:
                        target = root / 'target'
                        if kind == 'symlink':
                            target.write_text('regular file\n')
                        path.symlink_to(target)
                    with self.assertRaisesRegex(RuntimeError, 'deployment bind source has wrong type'):
                        restore_drill.web_override_mounts(work, services_for(work))


class CoachSafetyPortTest(unittest.TestCase):
    """Backup and restore guards ported from Cub-HQ/fitness-coach
    runtime/tests/test_wger_tool.py at f6d3efe32e27353defab8fc9a627094af98bfdbd
    (SnapshotTests), which covered this code before it moved here."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name)

    def snapshot(self, *extra):
        root = self.root / 'snapshot'
        root.mkdir()
        for name in (*REQUIRED, *extra):
            (root / name).write_bytes(name.encode())
        return root, backup.write_manifest(root, 'fixture')

    def test_manifest_requires_and_verifies_powersync_state(self):
        # Source: test_manifest_requires_and_verifies_powersync_state
        root, manifest = self.snapshot()
        self.assertTrue(manifest['includes_powersync_storage'])
        self.assertEqual(manifest['powersync_storage_source'], 'database.dump')
        self.assertEqual(RESTORE_ARTIFACTS, ('database.dump', 'media.tar'))
        self.assertNotIn('powersync.dump', manifest['files'])
        backup.verify_snapshot(root)
        (root / 'database.dump').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            backup.verify_snapshot(root)

    def test_migration_bundle_is_encrypted_and_privately_receipted(self):
        # Source: test_migration_bundle_is_encrypted_and_privately_receipted
        root, _ = self.snapshot('images.json')
        passphrase = self.root / 'passphrase'
        passphrase.write_text('x' * 32)
        passphrase.chmod(0o600)
        output = self.root / 'migration.dmg'

        def create(command, **kwargs):
            self.assertEqual(command[:2], ['/usr/bin/hdiutil', 'create'])
            self.assertEqual(command[command.index('-encryption') + 1], 'AES-256')
            self.assertEqual(kwargs['input'], b'x' * 32)
            output.write_bytes(b'encrypted fixture')

        receipt_path = backup.encrypt_snapshot(root, output, passphrase, runner=create)
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt['sha256'], hashlib.sha256(b'encrypted fixture').hexdigest())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)

    def test_restore_waits_for_named_database(self):
        # Source: test_restore_waits_for_named_database
        calls = []

        def ready(*command, data=None):
            calls.append(command)
            if len(calls) == 1:
                raise restore_drill.subprocess.CalledProcessError(1, command)
            return b'ok'

        with patch.object(restore_drill, 'run', side_effect=ready), patch.object(restore_drill.time, 'sleep'):
            restore_drill.wait_for_database('fixture', attempts=2, delay=0)
        self.assertEqual(calls[-1], ('exec', 'fixture-db', 'pg_isready', '-h', '127.0.0.1', '-U', 'restore', '-d', 'wger'))

    def test_restore_database_timeout_fails_explicitly(self):
        # Source: test_restore_database_timeout_fails_explicitly
        error = restore_drill.subprocess.CalledProcessError(1, ['pg_isready'])
        with patch.object(restore_drill, 'run', side_effect=error), patch.object(restore_drill.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'did not become ready'):
                restore_drill.wait_for_database('fixture', attempts=2, delay=0)


class DatabaseRecoveryProofTest(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(':memory:')
        self.addCleanup(self.database.close)
        self.database.execute('CREATE TABLE django_migrations (app TEXT, name TEXT)')
        self.database.execute("INSERT INTO django_migrations VALUES ('manager', '0030_baseline')")
        for table in ('User', 'WorkoutSession', 'WorkoutLog', 'ExerciseVideo'):
            self.database.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
            self.database.execute(f'INSERT INTO {table} VALUES (1)')
        # Real protected tables whose values DATABASE_PROOF digests; rows match the counted models.
        self.database.execute('CREATE TABLE manager_workoutsession (id INTEGER PRIMARY KEY, date TEXT, notes TEXT)')
        self.database.execute("INSERT INTO manager_workoutsession VALUES (1, '2026-09-01', 'synthetic private note')")
        self.database.execute('CREATE TABLE manager_workoutlog (id INTEGER PRIMARY KEY, session_id INTEGER, weight REAL, repetitions INTEGER)')
        self.database.execute('INSERT INTO manager_workoutlog VALUES (1, 1, 80.0, 5)')

    def install_recovery(self, database=None):
        database = database if database is not None else self.database
        database.execute('CREATE TABLE manager_workoutsessionrecovery (id INTEGER PRIMARY KEY, payload TEXT)')
        database.executemany('INSERT INTO manager_workoutsessionrecovery VALUES (?, ?)',
                             [(1, 'private synthetic draft'), (2, 'private synthetic completed draft')])
        database.execute("INSERT INTO django_migrations VALUES ('manager', '0031_workoutsessionrecovery')")

    def proof(self, database=None, introspection_error=None):
        database = database if database is not None else self.database

        def table_names():
            if introspection_error is not None:
                raise introspection_error
            return [row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]

        def get_model(app, model):
            return types.SimpleNamespace(objects=types.SimpleNamespace(
                count=lambda: database.execute(f'SELECT COUNT(*) FROM {model}').fetchone()[0]))

        def table_description(cursor, table):
            return [types.SimpleNamespace(name=row[1]) for row in database.execute(f'PRAGMA table_info({table})')]

        modules = {
            'django.apps': types.SimpleNamespace(apps=types.SimpleNamespace(get_model=get_model)),
            'django.db': types.SimpleNamespace(connection=types.SimpleNamespace(
                introspection=types.SimpleNamespace(table_names=table_names, get_table_description=table_description),
                cursor=lambda: contextlib.closing(database.cursor()),
                ops=types.SimpleNamespace(quote_name=lambda name: '"' + name + '"'))),
            'django.db.migrations.recorder': types.SimpleNamespace(MigrationRecorder=types.SimpleNamespace(
                Migration=types.SimpleNamespace(objects=types.SimpleNamespace(
                    values_list=lambda *fields: database.execute('SELECT app, name FROM django_migrations').fetchall())))),
        }
        output = io.StringIO()
        with patch.dict('sys.modules', modules), contextlib.redirect_stdout(output):
            exec(preflight.DATABASE_PROOF, {})
        raw = output.getvalue()
        with patch.object(preflight, '_command', return_value=raw.encode()):
            result = preflight._database(['docker'], 'synthetic-web', {})
        self.assertNotIn('private synthetic', raw)
        self.assertNotIn('synthetic private note', raw)
        return result

    def test_legacy_table_absence_allows_initial_rollout(self):
        self.assertEqual(self.proof()['counts']['recoveries'], 0)

    def test_restored_synthetic_recovery_rows_are_counted_without_payloads(self):
        self.install_recovery()
        self.database.commit()
        restored = sqlite3.connect(':memory:')
        self.addCleanup(restored.close)
        self.database.backup(restored)
        baseline = self.proof()
        self.assertEqual(baseline['counts']['recoveries'], 2)
        self.assertEqual(self.proof(restored), baseline)
        restored.execute('DELETE FROM manager_workoutsessionrecovery WHERE id = 1')
        self.assertNotEqual(self.proof(restored)['counts'], baseline['counts'])

    def test_migrated_missing_table_fails_closed(self):
        self.install_recovery()
        self.database.execute('DROP TABLE manager_workoutsessionrecovery')
        with self.assertRaisesRegex(RuntimeError, 'recovery.*table'):
            self.proof()

    def test_introspection_failure_is_not_legacy_absence(self):
        with self.assertRaisesRegex(RuntimeError, 'introspection unavailable'):
            self.proof(introspection_error=RuntimeError('introspection unavailable'))

    def test_existing_recovery_table_query_failure_fails_closed(self):
        self.install_recovery()
        self.database.set_authorizer(lambda action, table, *args:
                                     sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ
                                     and table == 'manager_workoutsessionrecovery' else sqlite3.SQLITE_OK)
        with self.assertRaises(sqlite3.DatabaseError):
            self.proof()

    def restored_copy(self):
        self.database.commit()
        restored = sqlite3.connect(':memory:')
        self.addCleanup(restored.close)
        self.database.backup(restored)
        return restored

    def test_candidate_edit_of_protected_row_refuses_even_with_equal_counts(self):
        baseline = self.proof()
        for statement, table in (('UPDATE manager_workoutlog SET weight = 82.5 WHERE id = 1', 'manager_workoutlog'),
                                 ("UPDATE manager_workoutsession SET notes = NULL WHERE id = 1", 'manager_workoutsession'),
                                 ('UPDATE manager_workoutlog SET id = 7 WHERE id = 1', 'manager_workoutlog'),
                                 ('ALTER TABLE manager_workoutsession DROP COLUMN notes', 'manager_workoutsession')):
            with self.subTest(statement=statement):
                restored = self.restored_copy()
                restored.execute(statement)
                candidate = self.proof(restored)
                self.assertEqual(candidate['counts'], baseline['counts'])
                with self.assertRaisesRegex(RuntimeError, 'candidate restore changed protected ' + table + ' rows'):
                    preflight._protected_unchanged(baseline, candidate)

    def test_candidate_added_column_and_untouched_rows_pass(self):
        baseline = self.proof()
        restored = self.restored_copy()
        restored.execute('ALTER TABLE manager_workoutlog ADD COLUMN distance REAL')
        preflight._protected_unchanged(baseline, self.proof(restored))

    def test_protected_rows_must_match_counted_models(self):
        self.database.execute('INSERT INTO manager_workoutlog VALUES (2, 1, 60.0, 8)')
        with self.assertRaisesRegex(RuntimeError, 'incomplete protected session/set proof'):
            self.proof()

    def test_validator_requires_nonnegative_integer_recovery_count(self):
        baseline = self.proof()
        for count in (None, -1, True, '2'):
            with self.subTest(count=count):
                proof = {**baseline, 'counts': dict(baseline['counts'])}
                if count is None:
                    proof['counts'].pop('recoveries')
                else:
                    proof['counts']['recoveries'] = count
                with patch.object(preflight, '_command', return_value=json.dumps(proof).encode()):
                    with self.assertRaisesRegex(RuntimeError, 'incomplete database proof'):
                        preflight._database(['docker'], 'synthetic-web', {})


class DrillRouteTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        self.socket = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.socket.close)
        self.socket.bind(str(self.root / 'docker.sock'))
        self.host = 'unix://' + str(self.root / 'docker.sock')
        self.environment = patch.dict(os.environ, {'WGER_DOCKER_HOST': self.host})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def cleanup_route(self, recovery=False, mismatch=False):
        name = ('wger-recovery-' if recovery else 'wger-restore-') + '0123456789'
        receipt = {'project': name, 'created_containers': [name + '-db', name + '-web', name + '-nginx'],
                   'volumes': [name + '-db', name + '-media', name + '-static'],
                   'network': name, 'networks': [name, name + '-front']}
        path = self.root / 'receipt.json'
        path.write_text(json.dumps(receipt))
        calls = []
        label = 'fitness.backup.' + ('recovery-drill' if recovery else 'restore-drill')

        def command(argv, **kwargs):
            calls.append(argv)
            self.assertEqual(argv[:3], ['docker', '-H', self.host])
            self.assertEqual(kwargs.get('env', os.environ)['WGER_DOCKER_HOST'], self.host)
            args = argv[3:]
            if 'ls' in args:
                key = {'container': 'created_containers', 'volume': 'volumes', 'network': 'networks'}[args[0]]
                return '\n'.join(receipt[key])
            payload = json.dumps([{'Config': {'Labels': {label: 'other' if mismatch else name}},
                                   'Labels': {label: 'other' if mismatch else name}}])
            if recovery:
                return payload.encode()
            return types.SimpleNamespace(stdout=payload, returncode=0)

        script = 'cleanup-recovery.py' if recovery else 'cleanup-drill.py'
        with patch('sys.argv', [script, str(path)]), patch('subprocess.run', side_effect=command), patch('subprocess.check_output', side_effect=command):
            if mismatch:
                with self.assertRaisesRegex(ValueError, 'owned|ownership'):
                    runpy.run_path(str(pathlib.Path(__file__).with_name(script)), run_name='__main__')
                self.assertFalse(any('rm' in call for call in calls))
            else:
                runpy.run_path(str(pathlib.Path(__file__).with_name(script)), run_name='__main__')
                self.assertEqual({call[-1] for call in calls if 'rm' in call},
                                 set(receipt['created_containers'] + receipt['volumes'] + receipt['networks']))

    def test_cleanup_routes_pin_every_inspection_and_removal(self):
        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                self.cleanup_route(recovery)

    def test_cleanup_routes_preserve_ownership_refusal(self):
        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                self.cleanup_route(recovery, mismatch=True)

    CANDIDATE = 'sha256:f3bfc71c7693fef7baaccaf097d3a2ca2ba9076ce547b90d777e5b822f74c008'

    def restore(self, *options, resolved=None):
        deploy = self.root / 'deploy'
        (deploy / 'config').mkdir(parents=True, exist_ok=True)
        (deploy / 'overrides').mkdir(exist_ok=True)
        (deploy / 'compose.yaml').write_text('services: {}')
        (deploy / 'config/private.env').write_text('PRIVATE=yes\n')
        snapshot = self.root / 'snapshot'
        snapshot.mkdir(exist_ok=True)
        backup.write_deployment_archive(snapshot / 'deployment.tar', deploy=deploy)
        for name in ('database.dump', 'media.tar'):
            (snapshot / name).write_bytes(b'fixture')
        (snapshot / 'manifest.json').write_text('{"files": {}}')
        manifest = (snapshot / 'manifest.json').read_bytes()
        calls = []

        def command(argv, **kwargs):
            calls.append(argv)
            self.assertEqual(argv[:3], ['docker', '-H', self.host])
            self.assertEqual(kwargs.get('env', os.environ)['WGER_DOCKER_HOST'], self.host)
            if argv[3:5] == ['image', 'inspect']:
                return types.SimpleNamespace(stdout=((resolved or argv[-1]) + '\n').encode())
            services = services_for(deploy, ())
            services.update(db={'image': 'postgres:15'}, nginx={'image': 'nginx:alpine'})
            return types.SimpleNamespace(stdout=json.dumps({'services': services}).encode())

        previous = set(self.root.glob('wger-restore-*'))
        with patch('sys.argv', ['restore-drill.py', str(snapshot), *options]), patch('subprocess.run', side_effect=command):
            restore_drill.main()
        self.assertEqual((snapshot / 'manifest.json').read_bytes(), manifest)
        [work] = set(self.root.glob('wger-restore-*')) - previous
        receipt = json.loads((work / 'receipt.json').read_text())
        return calls, receipt

    @staticmethod
    def web_images(calls):
        # Media extraction (image then -C) and the migrating web container (image then -c).
        return [call[call.index(flag) - 1] for call in calls if call[3] == 'run'
                for flag in ('-C', '-c') if flag in call]

    def test_restore_pins_every_docker_command(self):
        calls, receipt = self.restore()
        self.assertEqual({call[3] for call in calls}, {'compose', 'network', 'volume', 'run', 'exec'})
        self.assertEqual(receipt['state'], 'restored-awaiting-independent-application-check')

    def test_default_and_explicit_snapshot_restore_use_backup_image(self):
        backup_image = services_for(self.root)['web']['image']
        for options in ((), ('--snapshot-image',)):
            with self.subTest(options=options):
                calls, receipt = self.restore(*options)
                self.assertEqual(self.web_images(calls), [backup_image, backup_image])
                self.assertNotIn(self.CANDIDATE, sum(calls, []))
                self.assertEqual((receipt['image_source'], receipt['web_image'], receipt['snapshot_web_image']),
                                 ('snapshot', backup_image, backup_image))

    def test_candidate_restore_migrates_with_candidate_not_backup_image(self):
        backup_image = services_for(self.root)['web']['image']
        calls, receipt = self.restore('--candidate-image', self.CANDIDATE)
        self.assertEqual(self.web_images(calls), [self.CANDIDATE, self.CANDIDATE])
        migrate = next(call for call in calls if '/bin/sh' in call)
        self.assertIn('manage.py migrate', migrate[-1])
        self.assertNotIn(backup_image, [argument for call in calls for argument in call if call[3] == 'run'])
        self.assertEqual((receipt['image_source'], receipt['web_image'], receipt['snapshot_web_image']),
                         ('candidate', self.CANDIDATE, backup_image))

    def test_invalid_candidate_refuses_before_any_restore_resource(self):
        for image, resolved, cause in (('fitness-wger-backend:latest', None, 'immutable'),
                                        ('sha256:f3bfc71c', None, 'immutable'),
                                        (self.CANDIDATE.upper(), None, 'immutable'),
                                        (self.CANDIDATE, 'sha256:' + '2' * 64, 'does not resolve')):
            with self.subTest(image=image):
                with self.assertRaisesRegex(ValueError, cause):
                    self.restore('--candidate-image', image, resolved=resolved)
                self.assertEqual(list(self.root.glob('wger-restore-*')), [])

    def test_candidate_and_snapshot_modes_are_exclusive(self):
        with self.assertRaises(SystemExit):
            self.restore('--candidate-image', self.CANDIDATE, '--snapshot-image')
        self.assertEqual(list(self.root.glob('wger-restore-*')), [])

    def test_recovery_creation_and_cleanup_use_same_selected_endpoint(self):
        deploy = self.root / 'deploy'
        (deploy / 'config').mkdir(parents=True)
        (deploy / 'overrides').mkdir()
        (deploy / 'compose.yaml').write_text('services: {}')
        (deploy / 'config/private.env').write_text('PRIVATE=yes\n')
        snapshot = self.root / 'snapshot'
        snapshot.mkdir()
        backup.write_deployment_archive(snapshot / 'deployment.tar', deploy=deploy)
        for name in ('database.dump', 'media.tar'):
            (snapshot / name).write_bytes(b'fixture')
        backup.write_manifest(snapshot, 'synthetic')
        calls = []
        resources = {'container': {}, 'volume': {}, 'network': {}}

        def command(argv, **kwargs):
            argv = list(argv)
            calls.append(argv)
            self.assertEqual(argv[:3], ['docker', '-H', self.host])
            args = argv[3:]
            payload = b''
            if args[0] == 'compose':
                services = services_for(deploy, ())
                services.update(db={'image': 'postgres:15'}, powersync={'image': 'powersync:fixture'})
                payload = json.dumps({'services': services}).encode()
            elif args[0] == 'run' or args[:2] in (['network', 'create'], ['volume', 'create']):
                label = args[args.index('--label') + 1].split('=', 1)
                if args[0] != 'run':
                    resources[args[0]][args[-1]] = dict([label])
                elif '--name' in args:
                    resources['container'][args[args.index('--name') + 1]] = dict([label])
            elif args[0] == 'inspect':
                payload = b'true\n'
            elif args[0] == 'rm':
                del resources['container'][args[-1]]
            elif args[1:2] == ['rm']:
                del resources[args[0]][args[-1]]
            return types.SimpleNamespace(stdout=payload)

        def output(argv, **kwargs):
            self.assertEqual(argv[:3], ['docker', '-H', self.host])
            calls.append(argv)
            kind, action = argv[3:5]
            if action == 'ls':
                return '\n'.join(resources[kind])
            labels = resources[kind][argv[-1]]
            return json.dumps([{'Config': {'Labels': labels}, 'Labels': labels}]).encode()

        with patch('sys.argv', ['recovery-drill.py', str(snapshot)]), patch('subprocess.run', side_effect=command), patch('time.sleep'), patch('os.umask'):
            runpy.run_path(str(pathlib.Path(__file__).with_name('recovery-drill.py')), run_name='__main__')
        self.assertEqual({call[3] for call in calls}, {'compose', 'network', 'volume', 'run', 'exec', 'inspect', 'logs'})
        receipt_path = next(self.root.glob('wger-recovery-*/receipt.json'))
        receipt = json.loads(receipt_path.read_text())
        self.assertTrue(receipt['powersync_container_running'])
        self.assertEqual(set(resources['container']), set(receipt['created_containers']))
        self.assertEqual(set(resources['volume']), set(receipt['volumes']))
        self.assertEqual(set(resources['network']), set(receipt['networks']))
        with patch('sys.argv', ['cleanup-recovery.py', str(receipt_path)]), patch('subprocess.run', side_effect=command), patch('subprocess.check_output', side_effect=output):
            runpy.run_path(str(pathlib.Path(__file__).with_name('cleanup-recovery.py')), run_name='__main__')
        self.assertEqual(resources, {'container': {}, 'volume': {}, 'network': {}})
        backup.verify_snapshot(snapshot)

    def test_backup_commands_use_selected_endpoint_and_default_only_when_absent(self):
        for host in (self.host, None):
            with self.subTest(host=host), patch.dict(os.environ):
                if host is None:
                    os.environ.pop('WGER_DOCKER_HOST', None)
                    default = self.root / '.colima/default/docker.sock'
                    default.parent.mkdir(parents=True)
                    with socket.socket(socket.AF_UNIX) as sock:
                        sock.bind(str(default))
                        self.assert_backup_endpoint('unix://' + str(default))
                else:
                    self.assert_backup_endpoint(host)

    def test_snapshot_pins_capture_and_writer_recovery_commands(self):
        deploy = self.root / 'deploy'
        (deploy / 'config').mkdir(parents=True)
        (deploy / 'overrides').mkdir()
        (deploy / 'compose.yaml').write_text('services: {}')
        (deploy / 'config/private.env').write_text('PRIVATE=yes\n')
        destination = self.root / 'fitness-coach-migration'
        for name in ('writer.lock', 'history.lock'):
            (self.root / name).touch()
        calls = []

        def command(argv, **kwargs):
            if argv[0] != 'docker':
                raise AssertionError(argv)
            calls.append(argv)
            self.assertEqual(argv[:3], ['docker', '-H', self.host])
            self.assertEqual(kwargs.get('env', os.environ)['WGER_DOCKER_HOST'], self.host)
            args = argv[3:]
            if args[0] == 'inspect':
                payload = json.dumps([{'Image': args[1], 'State': {'Status': 'running'}}]).encode()
            elif args[0] == 'compose' and args[3:6] == ['ps', '-a', '-q']:
                payload = args[-1].encode()
            else:
                payload = b''
            return types.SimpleNamespace(stdout=payload)

        with patch.object(pathlib.Path, 'home', return_value=self.root), patch.object(backup, 'DEPLOY', deploy), patch.dict(os.environ, {'WGER_WRITER_LOCK': str(self.root / 'writer.lock'), 'WGER_HISTORY_LOCK': str(self.root / 'history.lock')}), patch('subprocess.run', side_effect=command), patch.object(backup, 'write_deployment_archive'), patch.object(backup, 'write_manifest'):
            snapshot = backup.snapshot(destination)
        self.assertEqual(json.loads((destination / 'latest.json').read_text())['snapshot'], str(snapshot))
        self.assertEqual({call[3] for call in calls}, {'compose', 'inspect', 'stop', 'start', 'run'})
        self.assertIn(['docker', '-H', self.host, 'compose', '-f', str(deploy / 'compose.yaml'),
                       'exec', '-T', 'nginx', 'nginx', '-s', 'reload'], calls)

    def assert_backup_endpoint(self, host):
        with patch.object(pathlib.Path, 'home', return_value=self.root), patch('subprocess.run', return_value=types.SimpleNamespace(stdout=b'')) as command:
            backup.docker('stop', 'writer')
            backup.compose('ps', '-a')
            backup.media_inventory()
        for call in command.call_args_list:
            self.assertEqual(call.args[0][:3], ['docker', '-H', host])

    def test_invalid_endpoints_refuse_before_docker_or_cleanup(self):
        regular = self.root / 'regular'; regular.touch()
        endpoints = ('', 'tcp://127.0.0.1:2375', 'unix://', 'unix://relative.sock',
                     'unix://' + str(self.root / 'missing.sock'), 'unix://' + str(regular))
        for endpoint in endpoints:
            for route in ('backup', 'restore-drill.py', 'recovery-drill.py', 'cleanup-drill.py', 'cleanup-recovery.py'):
                with self.subTest(endpoint=endpoint, route=route), patch.dict(os.environ, {'WGER_DOCKER_HOST': endpoint}), patch('subprocess.run') as command, patch('subprocess.check_output') as output:
                    with self.assertRaisesRegex(RuntimeError, 'Docker (endpoint|socket)'):
                        if route == 'backup':
                            backup.docker('stop', 'writer')
                        else:
                            with patch('sys.argv', [route, str(self.root / 'absent')]):
                                runpy.run_path(str(pathlib.Path(__file__).with_name(route)), run_name='__main__')
                    command.assert_not_called()
                    output.assert_not_called()


class PlanRouteTest(unittest.TestCase):
    def test_backup_precedes_installed_fork_sync(self):
        plan=pathlib.Path(__file__).parents[1].joinpath('issue-83-deployment-plan.txt').read_text()
        self.assertIn('main after merged PR #112',plan)
        self.assertLess(plan.index('Run the reviewed `backup.py`'),plan.index('Only after backup and restore proof pass, sync'))
        self.assertIn('never use `compose up`',plan)

if __name__=='__main__':unittest.main()
