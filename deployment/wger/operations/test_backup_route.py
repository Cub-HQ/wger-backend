import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import backup


class BackupRouteTest(unittest.TestCase):
    def test_native_colima_closed_writer_snapshot_order_and_custody(self):
        source=pathlib.Path(backup.__file__).read_text()
        self.assertNotIn('limactl',source);self.assertIn('.colima/docker.sock',source)
        snapshot=source[source.index('def snapshot('):]
        for token in ('WGER_WRITER_LOCK','WGER_HISTORY_LOCK'):self.assertIn(token,snapshot)
        self.assertIn('fcntl.flock(handle,operation)',source);self.assertIn('fcntl.lockf(handle,operation)',source)
        stop=snapshot.index("docker('stop',*running_ids)")
        capture=[snapshot.index(token) for token in ("'pg_dump --format=custom","'media.tar'","'deployment.tar'","'images.json'")]
        resume=snapshot.index("docker('start',*running_ids")
        self.assertTrue(all(stop<item<resume for item in capture))

    def test_exact_existing_containers_resume_before_latest_publication(self):
        source=pathlib.Path(backup.__file__).read_text();snapshot=source[source.index('def snapshot('):]
        self.assertIn("compose('ps','-a','-q',service)",snapshot);self.assertNotIn("compose('up'",snapshot)
        self.assertIn('wait_for_state(ident,writer_states[service])',snapshot)
        self.assertLess(snapshot.index('wait_for_state('),snapshot.index("latest.json"))
        self.assertIn("(root/'INCOMPLETE').touch()",snapshot)

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
                raise AssertionError(args)
            starts=[]
            def docker(*args,**kwargs):
                if args[0]=='stop':return b''
                if args[0]=='start':starts.extend(args[1:]);return b''
                raise AssertionError(args)
            with patch.object(backup.pathlib.Path,'home',return_value=home),patch.object(backup,'DEPLOY',home),patch.object(backup,'DOCKER_HOST','unix:///tmp/proof.sock'),patch.object(backup.pathlib.Path,'is_socket',return_value=True),patch.object(backup,'compose',side_effect=compose),patch.object(backup,'docker',side_effect=docker),patch.object(backup,'inspect_state',side_effect=lambda ident:states[ident.removeprefix('id-')]),patch.object(backup,'media_inventory',return_value=b''),patch.dict(os.environ,{'WGER_WRITER_LOCK':str(native),'WGER_HISTORY_LOCK':str(state)}):
                (home/'compose.yaml').touch()
                with self.assertRaises(RuntimeError):backup.snapshot(destination)
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
