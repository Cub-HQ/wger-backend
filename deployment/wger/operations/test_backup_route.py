import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sqlite3
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

import backup
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
                with patch.object(backup.subprocess, 'run', return_value=result):
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
            with patch.object(backup.pathlib.Path,'home',return_value=home),patch.object(backup,'DEPLOY',home),patch.object(backup,'DOCKER_HOST','unix:///tmp/proof.sock'),patch.object(backup.pathlib.Path,'is_socket',return_value=True),patch.object(backup,'compose',side_effect=compose),patch.object(backup,'docker',side_effect=docker),patch.dict(os.environ,{'WGER_WRITER_LOCK':str(native),'WGER_HISTORY_LOCK':str(state)}):
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
            with patch.object(backup.pathlib.Path,'home',return_value=home),patch.object(backup,'DEPLOY',home),patch.object(backup,'DOCKER_HOST','unix:///tmp/proof.sock'),patch.object(backup.pathlib.Path,'is_socket',return_value=True),patch.object(backup,'compose',side_effect=compose),patch.object(backup,'docker',side_effect=docker),patch.object(backup,'inspect_state',side_effect=lambda ident:states[ident.removeprefix('id-')]),patch.object(backup,'media_inventory',return_value=b''),patch.dict(os.environ,{'WGER_WRITER_LOCK':str(native),'WGER_HISTORY_LOCK':str(state)}):
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
            with patch.object(backup.pathlib.Path,'home',return_value=home),patch.object(backup,'DEPLOY',home),patch.object(backup,'DOCKER_HOST','unix:///tmp/proof.sock'),patch.object(backup.pathlib.Path,'is_socket',return_value=True),patch.object(backup,'compose',side_effect=compose),patch.object(backup,'docker',side_effect=docker),patch.object(backup,'inspect_state',side_effect=lambda ident:states[ident.removeprefix('id-')]),patch.object(backup,'wait_for_state',side_effect=wait_for_state),patch.object(backup,'media_inventory',return_value=b''),patch.dict(os.environ,{'WGER_WRITER_LOCK':str(native),'WGER_HISTORY_LOCK':str(state)}):
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
            with patch('sys.argv', ['restore-drill.py', str(snapshot)]), patch.object(restore_drill, 'deployment_services', return_value=services_for(deploy, ())), patch.object(restore_drill, 'run', side_effect=RuntimeError('stop before Docker')):
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



class DatabaseRecoveryProofTest(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(':memory:')
        self.addCleanup(self.database.close)
        self.database.execute('CREATE TABLE django_migrations (app TEXT, name TEXT)')
        self.database.execute("INSERT INTO django_migrations VALUES ('manager', '0030_baseline')")
        for table in ('User', 'WorkoutSession', 'WorkoutLog', 'ExerciseVideo'):
            self.database.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
            self.database.execute(f'INSERT INTO {table} VALUES (1)')

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

        modules = {
            'django.apps': types.SimpleNamespace(apps=types.SimpleNamespace(get_model=get_model)),
            'django.db': types.SimpleNamespace(connection=types.SimpleNamespace(
                introspection=types.SimpleNamespace(table_names=table_names),
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
    def test_restore_and_cleanup_pin_colima_socket_without_duplicate_binary(self):
        root=pathlib.Path(__file__).parent;restore=(root/'restore-drill.py').read_text();cleanup=(root/'cleanup-drill.py').read_text()
        self.assertIn("DOCKER=('docker','-H',DOCKER_HOST)",restore);self.assertIn("subprocess.run([*DOCKER,*a]",restore)
        self.assertNotIn("run('docker'",restore)
        for command in ("run('network','create'","run('volume','create'","run('run','-d'","run('exec'"):
            self.assertIn(command,restore)
        self.assertIn("DOCKER=['docker','-H'",cleanup);self.assertIn("[*DOCKER,kind,'inspect',ident]",cleanup)
        self.assertNotIn("'network','ls'",cleanup);self.assertIn('fitness.backup.restore-drill',cleanup)


class PlanRouteTest(unittest.TestCase):
    def test_backup_precedes_installed_fork_sync(self):
        plan=pathlib.Path(__file__).parents[1].joinpath('issue-83-deployment-plan.txt').read_text()
        self.assertIn('main after merged PR #112',plan)
        self.assertLess(plan.index('Run the reviewed `backup.py`'),plan.index('Only after backup and restore proof pass, sync'))
        self.assertIn('never use `compose up`',plan)

if __name__=='__main__':unittest.main()
