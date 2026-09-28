"""prepare-react.sh --backend-image against fake docker/curl and a real git archive of a fixture repo.

The fixture repo carries the real committed frontend package, and the fake build unpacks it the way
the Dockerfile does, so image verification runs over the actual reviewed package bytes."""
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).parent
SCRIPT = (ROOT / 'prepare-react.sh').read_text()
MATERIAL = re.search(r"^BACKEND_MATERIAL='([^']*)'", SCRIPT, re.MULTILINE).group(1).split()
PACKAGE = re.search(r'^FRONTEND_PACKAGE=(\S+)$', SCRIPT, re.MULTILINE).group(1)
UI_COMMIT = re.search(r'^FRONTEND_COMMIT=(\S+)$', SCRIPT, re.MULTILINE).group(1)
INSTALLED = 'node_modules/@wger-project/react-components'
FAKE_DOCKER = f'#!{sys.executable}\n' + r'''import json, os, re, shutil, subprocess, sys, tarfile
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ['COMMAND_LOG']).open('a') as log:
    log.write(json.dumps(args) + '\n')
assert args[:2] == ['-H', os.environ['WGER_DOCKER_HOST']], args
args = args[2:]
state_path = Path(os.environ['DOCKER_STATE'])
state = json.loads(state_path.read_text())
def save(): state_path.write_text(json.dumps(state))
def image(ref): return state['images'].get(state['tags'].get(ref, ref))
if args[0] == 'info':
    pass
elif args[:2] == ['image', 'inspect']:
    ref = args[-1]
    if ref == 'wger/base:latest':
        print('wger/base@sha256:' + 'b' * 64)
        sys.exit(0)
    found = image(ref)
    if found is None: sys.exit(1)
    fmt = args[args.index('--format') + 1] if '--format' in args else ''
    if fmt == '{{.Id}}': print(state['tags'].get(ref, ref))
    elif 'Labels' in fmt: print(found['labels'].get(fmt.split('"')[1], ''))
    elif 'Env' in fmt: print('APP_BUILD_COMMIT=' + found['commit'])
elif args[0] == 'pull':
    pass
elif args[0] == 'build':
    context = Path(args[-1])
    assert context.is_relative_to(Path(os.environ['HOME']) / '.cache'), context
    image_id = 'sha256:' + str(len(state['images'])).rjust(64, 'f')
    root = Path(os.environ['IMAGES']) / image_id.removeprefix('sha256:')
    shutil.copytree(context, root)
    # The Dockerfile's builder stage: unpack the committed package with --strip-components=1.
    with tarfile.open(root / os.environ['PACKAGE']) as archive:
        for member in archive.getmembers():
            if member.isfile():
                target = root / os.environ['INSTALLED'] / member.name.split('/', 1)[1]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
    if os.environ.get('CORRUPT_BUILD'): (root / os.environ['CORRUPT_BUILD']).write_text('drift')
    labels = dict(value.split('=', 1) for flag, value in zip(args, args[1:]) if flag == '--label')
    commit = next(value.split('=', 1)[1] for flag, value in zip(args, args[1:])
                  if flag == '--build-arg' and value.startswith('BUILD_COMMIT='))
    dockerfile = Path(args[args.index('--file') + 1]).read_text()
    ui_commit = re.search(r'^ARG UI_BUILD_COMMIT=(\S+)$', dockerfile, re.MULTILINE).group(1)
    state['images'][image_id] = {'root': str(root), 'labels': labels, 'commit': commit, 'ui_commit': ui_commit}
    save()
    Path(args[args.index('--iidfile') + 1]).write_text(image_id)
elif args[0] == 'run':
    found = image(args[args.index('--entrypoint') + 2])
    payload = args[args.index('--entrypoint') + 3:]
    payload = [found['root'] if arg == '/home/wger/src' else arg for arg in payload]
    env = {**os.environ, 'APP_BUILD_COMMIT': found['commit'], 'APP_UI_BUILD_COMMIT': found['ui_commit'],
           'PYTHONPATH': found['root']}
    sys.exit(subprocess.run([sys.executable, *payload], env=env, cwd=found['root']).returncode)
elif args[0] == 'tag':
    state['tags'][args[2]] = args[1]
    save()
else:
    sys.exit('unexpected docker call: ' + json.dumps(args))
'''
FAKE_CURL = f'#!{sys.executable}\n' + r'''import os, shutil, sys
args = sys.argv[1:]
assert args[-3].endswith('.tar.gz'), args
shutil.copy(os.environ['ARCHIVE'], args[-1])
'''


class BackendImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = root = pathlib.Path(self.tmp.name).resolve()
        repo = root / 'repo'
        for path in [*MATERIAL, 'wger/__init__.py', 'wger/formats/__init__.py', 'wger/formats/en_AU/__init__.py',
                     'wger/manager/__init__.py', 'wger/manager/models/__init__.py', 'settings/main.py']:
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(f'# {path}\n')
        (repo / 'extras/docker/production').mkdir(parents=True)
        (repo / 'extras/docker/production/Dockerfile').write_text(
            f'FROM wger/base:latest\nARG UI_BUILD_COMMIT={UI_COMMIT}\n')
        (repo / PACKAGE).parent.mkdir(parents=True)
        (repo / PACKAGE).write_bytes((ROOT.parents[2] / PACKAGE).read_bytes())
        git = ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t']
        subprocess.run([*git, 'init', '-q'], check=True)
        subprocess.run([*git, 'add', '.'], check=True)
        subprocess.run([*git, 'commit', '-qm', 'fixture'], check=True)
        self.commit = subprocess.run([*git, 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run([*git, 'archive', '--format=tar.gz', '--prefix=wger-backend/', '-o', str(root / 'source.tgz'), 'HEAD'], check=True)
        patches = root / 'deploy/patches'
        patches.mkdir(parents=True)
        script = (ROOT / 'prepare-react.sh').read_text()
        self.script = patches / 'prepare-react.sh'
        self.script.write_text(re.sub(r'^BACKEND_COMMIT=\S+$', 'BACKEND_COMMIT=' + self.commit, script, flags=re.MULTILINE))
        self.overrides = root / 'deploy/overrides'
        self.overrides.mkdir()
        (self.overrides / 'react-main.js.next').write_text('previous candidate')
        (self.overrides / 'react-main.js').write_text('live')
        binary = root / 'bin'
        binary.mkdir()
        for name, body in (('docker', FAKE_DOCKER), ('curl', FAKE_CURL)):
            (binary / name).write_text(body)
            (binary / name).chmod(0o755)
        (root / 'images').mkdir()
        (root / 'state.json').write_text(json.dumps({'images': {}, 'tags': {}}))
        (root / 'home').mkdir()
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(root / 'd.sock'))
        self.env = {**os.environ, 'HOME': str(root / 'home'), 'PATH': f'{binary}:{os.environ["PATH"]}',
                    'WGER_DOCKER_HOST': f'unix://{root}/d.sock', 'COMMAND_LOG': str(root / 'commands.jsonl'),
                    'DOCKER_STATE': str(root / 'state.json'), 'IMAGES': str(root / 'images'),
                    'ARCHIVE': str(root / 'source.tgz'), 'PACKAGE': PACKAGE, 'INSTALLED': INSTALLED}
        self.repo, self.git = repo, git

    def recommit(self, path, content):
        """Commit a change to the fixture source and point the script's pin at the new commit."""
        (self.repo / path).write_bytes(content)
        subprocess.run([*self.git, 'commit', '-qam', 'change'], check=True)
        old, self.commit = self.commit, subprocess.run([*self.git, 'rev-parse', 'HEAD'], check=True,
                                                       capture_output=True, text=True).stdout.strip()
        subprocess.run([*self.git, 'archive', '--format=tar.gz', '--prefix=wger-backend/',
                        '-o', str(self.root / 'source.tgz'), 'HEAD'], check=True)
        self.script.write_text(self.script.read_text().replace(old, self.commit))

    def tearDown(self):
        self.sock.close()
        self.tmp.cleanup()

    def prepare(self, **env):
        (self.root / 'commands.jsonl').write_text('')
        return subprocess.run(['bash', str(self.script), '--backend-image'], env={**self.env, **env},
                              capture_output=True, text=True)

    def calls(self):
        return [json.loads(line)[2:] for line in (self.root / 'commands.jsonl').read_text().splitlines()]

    def state(self):
        return json.loads((self.root / 'state.json').read_text())

    def record(self):
        return json.loads((self.overrides / 'backend-image.json.next').read_text())

    def assert_image_only(self, calls):
        verbs = {call[0] if call[0] != 'image' else 'image ' + call[1] for call in calls}
        self.assertLessEqual(verbs, {'info', 'image inspect', 'pull', 'build', 'run', 'tag'})
        for call in calls:
            if call[0] == 'run':
                self.assertEqual(call[call.index('--network') + 1], 'none')
                self.assertFalse({'--mount', '-v', '--volume', '--env-file'} & set(call), call)
        self.assertEqual((self.overrides / 'react-main.js').read_text(), 'live')
        self.assertEqual((self.overrides / 'react-main.js.next').read_text(), 'previous candidate')
        self.assertEqual(list((self.root / 'home/.cache').iterdir()), [], 'build directory left behind')

    def test_builds_verifies_tags_and_stages_provenance_then_reuses_verified_image(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assert_image_only(calls)
        tag = 'fitness-wger-backend:' + self.commit
        image_id = self.state()['tags'][tag]
        verbs = [call[0] for call in calls]
        self.assertLess(verbs.index('run'), verbs.index('tag'), 'tagged before content verification')
        build = next(call for call in calls if call[0] == 'build')
        self.assertIn(f'wger/base:latest=docker-image://wger/base@sha256:{"b" * 64}', build)
        record = self.record()
        self.assertEqual({key: record[key] for key in ('commit', 'image_id', 'tag', 'app_build_commit', 'base_image')},
                         {'commit': self.commit, 'image_id': image_id, 'tag': tag, 'app_build_commit': self.commit,
                          'base_image': 'wger/base@sha256:' + 'b' * 64})
        self.assertEqual(record['repository'], 'https://github.com/Cub-HQ/wger-backend')
        self.assertEqual(record['frontend'], {
            'repository': 'https://github.com/Cub-HQ/wger-frontend', 'commit': UI_COMMIT,
            'upstream': 'https://github.com/wger-project/react 89d234a800ba0f2097162f1d91444c7e3a5ccc5c',
            'package': PACKAGE,
            'package_sha256': 'eee46be70533c44e5b5f3da25fcd1e63c2abcb9e638063d35b23e07990ce2a0f',
            'main_js_sha256': '3bc9b51957e7c819dc48d2f381a922637de3042ab6b4108e31876299df2e2383'})
        self.assertIn('source and frontend package files', result.stdout + result.stderr)

        (self.overrides / 'backend-image.json.next').unlink()
        again = self.prepare()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertFalse({'build', 'pull', 'tag'} & {call[0] for call in self.calls()}, 'verified image rebuilt')
        self.assert_image_only(self.calls())
        self.assertEqual(self.record()['image_id'], image_id)

    def assert_refused(self, result, message):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)
        self.assertFalse((self.overrides / 'backend-image.json.next').exists())
        self.assertNotIn('tag', [call[0] for call in self.calls()])
        self.assert_image_only(self.calls())

    def test_archive_of_another_commit_is_refused_before_any_build(self):
        self.script.write_text(self.script.read_text().replace(self.commit, 'a' * 40))
        result = self.prepare()
        self.assert_refused(result, f"archive commit '{self.commit}' is not {'a' * 40}")
        self.assertFalse({'build', 'pull', 'run'} & {call[0] for call in self.calls()})

    def test_built_image_with_drifted_source_is_not_tagged(self):
        result = self.prepare(CORRUPT_BUILD='wger/manager/models/session_recovery.py')
        self.assert_refused(result, 'failed source verification; not tagged')
        self.assertIn('wger/manager/models/session_recovery.py', result.stderr)
        self.assertEqual(self.state()['tags'], {})

    def test_existing_tag_with_other_revision_or_content_is_never_overwritten(self):
        self.assertEqual(self.prepare().returncode, 0)
        (self.overrides / 'backend-image.json.next').unlink()
        state = self.state()
        image_id = state['tags']['fitness-wger-backend:' + self.commit]
        for case in ('revision', 'content'):
            with self.subTest(case=case):
                state = self.state()
                found = state['images'][image_id]
                if case == 'revision':
                    found['labels']['org.opencontainers.image.revision'] = 'c' * 40
                else:
                    found['labels']['org.opencontainers.image.revision'] = self.commit
                    pathlib.Path(found['root'], 'wger/utils/pdf.py').write_text('drift')
                (self.root / 'state.json').write_text(json.dumps(state))
                result = self.prepare()
                self.assert_refused(result, 'tag not overwritten')
                self.assertFalse({'build', 'pull'} & {call[0] for call in self.calls()})
                self.assertEqual(self.state()['tags'], {'fitness-wger-backend:' + self.commit: image_id})

    def test_source_missing_accepted_backend_file_is_refused(self):
        self.script.write_text(self.script.read_text().replace('wger/utils/pdf.py', 'wger/utils/absent.py', 1))
        result = self.prepare()
        self.assert_refused(result, 'source lacks accepted backend files: wger/utils/absent.py')
        self.assertFalse({'build', 'pull', 'run'} & {call[0] for call in self.calls()})

    def test_other_frontend_package_is_refused_before_any_build(self):
        # Same name/version from anywhere else (e.g. the upstream registry) is not the reviewed fork build.
        self.recommit(PACKAGE, (self.repo / PACKAGE).read_bytes() + b'\0')
        result = self.prepare()
        self.assert_refused(result, 'not the reviewed eee46be7')
        self.assertFalse({'build', 'pull', 'run'} & {call[0] for call in self.calls()})

    def test_image_serving_other_frontend_bytes_is_not_tagged(self):
        for corrupt in ('build/main.js', 'build/locales/de/translation.json', 'build/unreviewed.js'):
            with self.subTest(corrupt=corrupt):
                result = self.prepare(CORRUPT_BUILD=f'{INSTALLED}/{corrupt}')
                self.assert_refused(result, 'failed source verification; not tagged')
                self.assertIn(f'{INSTALLED}/{corrupt}', result.stderr)
                self.assertEqual(self.state()['tags'], {})

    def test_image_recording_other_frontend_commit_is_not_tagged(self):
        self.recommit('extras/docker/production/Dockerfile', b'FROM wger/base:latest\nARG UI_BUILD_COMMIT=' + b'e' * 40 + b'\n')
        result = self.prepare()
        self.assert_refused(result, f"APP_UI_BUILD_COMMIT is '{'e' * 40}', expected {UI_COMMIT}")
        self.assertEqual(self.state()['tags'], {})


if __name__ == '__main__':
    unittest.main()
