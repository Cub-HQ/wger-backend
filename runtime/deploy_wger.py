#!/usr/bin/env python3
"""Factory-only bridge from a merged source tree to the reviewed gym release."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

from deploy_failure import failure_reason

PREFIX = 'deployment/wger/'
# Explicit custody: never copy a directory recursively into the live deployment.
PRODUCT_FILES = (
    'compose.yaml', 'overrides/settings-main.py', 'overrides/manager-urls.py',
    'formats/en_AU/formats.py',
    'config/nginx.conf', 'config/powersync.yaml', 'config/sync_rules.yaml',
    'patches/prepare-react.sh', 'patches/patch_australian_dates.py',
    'patches/patch_australian_template_dates.py', 'patches/patch_australian_pdf.py',
    'patches/patch_progression_chart.py', 'patches/patch_muscle_diagram.py',
    'patches/patch_footer.py',
    'patches/patch_session_recovery.py', 'patches/patch_session_recovery_ui.py',
    'patches/test_patch_session_recovery_ui.py', 'patches/test_patch_session_recovery.py',
)
MACHINERY = ('patches/release-web.sh', 'patches/release_web.py', 'patches/release_env.py',
             'patches/setup-powersync-storage.py')
# These are executed from the merged source by wger_preflight before release. They
# are not copied into the live deployment, but a change to them is exercised by
# this route rather than requiring a separate operations activation.
PREFLIGHT_FILES = ('operations/backup.py', 'operations/snapshot.py',
                   'operations/restore-drill.py', 'operations/cleanup-drill.py')
# Operator-invoked source only: never copied or executed by the release adapter.
SOURCE_ONLY_FILES = ('operations/recovery-drill.py',)
OPERATIONS = ('operations/cleanup-recovery.py',
              'operations/com.cortana.fitness-wger.backup.plist',
              'com.cortana.fitness-wger.vm.plist')
EVIDENCE_FILES = ('.gitignore', 'issue-83-deployment-plan.txt', 'config/private.env.example',
                  'patches/test_release_route.py', 'patches/test_patch_progression_chart.py',
                  'patches/test_patch_ux_wave1.py', 'patches/test_patch_ux_wave3.py',
                  'patches/test_australian_dates.py', 'patches/check_pinned_artifacts.py',
                  'patches/test_powersync_storage.py', 'operations/test_backup_route.py')
RETIRED_FILES = ('patches/patch_server_wave3.py', 'patches/patch_ux_wave1.py',
                 'patches/patch_ux_wave3.py')
REMOVED_FILES = ('Dockerfile', 'settings-main.py')


def normalize_bundle(data):
    return re.sub(rb'sourceMappingURL=main\.js\.[0-9a-f]{12}\.map',
                  b'sourceMappingURL=main.js.map', data)


def check_surfaces(paths, source=None):
    for path in paths:
        if not path.startswith(PREFIX):
            raise ValueError('DEPLOY_MISSING: non-gym surface ' + path)
        relative = path[len(PREFIX):]
        if relative in REMOVED_FILES and source is not None:
            removed = Path(source) / path
            if not removed.exists() and not removed.is_symlink():
                continue
        if relative not in PRODUCT_FILES + MACHINERY + PREFLIGHT_FILES + SOURCE_ONLY_FILES + EVIDENCE_FILES:
            # Scheduler/cleanup activation remains separate. Retired release
            # inputs stay explicitly classified but cannot silently deploy.
            kind = ('operations activation' if relative in OPERATIONS else
                    'retired release input' if relative in RETIRED_FILES else 'unsupported surface')
            raise ValueError('DEPLOY_MISSING: ' + kind + ': ' + path)


def existing_locks():
    execution = Path.home() / 'orca/projects/Fitness Coach/imports/issue-29/lifetime-v6-final-supervised-20260916/execution'
    defaults = {'WGER_WRITER_LOCK': execution / 'training-writer.lock',
                'WGER_HISTORY_LOCK': execution / 'state/.training-writer.lock'}
    result = {}
    for key, default in defaults.items():
        path = Path(os.environ.get(key, default))
        if path.is_symlink() or not path.is_file():
            raise ValueError('DEPLOY_MISSING: existing custody lock unavailable: ' + str(path))
        result[key] = str(path)
    return result


def staging_root(home):
    uid = os.geteuid()
    writable_by_others = stat.S_IWGRP | stat.S_IWOTH
    try:
        home_stat = home.stat()
    except OSError as error:
        raise ValueError('DEPLOY_MISSING: resolved home unavailable') from error
    if not stat.S_ISDIR(home_stat.st_mode):
        raise ValueError('DEPLOY_MISSING: resolved home must be a directory')
    if home_stat.st_uid != uid or home_stat.st_mode & writable_by_others:
        raise ValueError('DEPLOY_MISSING: resolved home must be owned by the deployment uid and not group- or world-writable')
    cache = home / '.cache'
    try:
        if cache.is_symlink():
            raise ValueError('DEPLOY_MISSING: staging cache must not be a symlink')
        cache.mkdir(mode=0o700, exist_ok=True)
        resolved = cache.resolve(strict=True)
        if cache.is_symlink() or not resolved.is_dir() or home not in resolved.parents:
            raise ValueError('DEPLOY_MISSING: staging cache must be a directory beneath home')
        cache_stat = resolved.stat()
        if cache_stat.st_uid != uid or cache_stat.st_mode & writable_by_others:
            raise ValueError('DEPLOY_MISSING: staging cache must be owned by the deployment uid and not group- or world-writable')
    except (OSError, RuntimeError) as error:
        raise ValueError('DEPLOY_MISSING: staging cache unavailable beneath home') from error
    return resolved


def deploy(args):
    source = Path(args.source).resolve()
    check_surfaces(args.changed_file, source)
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if not re.fullmatch('[0-9a-f]{40}', args.commit) or revision != args.commit:
        raise ValueError('DEPLOY_MISSING: source HEAD does not match requested revision')
    if subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--', 'deployment/wger'], text=True).strip():
        raise ValueError('DEPLOY_MISSING: gym product source has uncommitted changes')
    live = Path(args.deploy_root).expanduser().resolve()
    if not (live / 'config/private.env').is_file():
        raise ValueError('DEPLOY_MISSING: existing private gym environment unavailable')
    env = {**os.environ, **existing_locks(), 'WGER_DEPLOY_DIR': str(live),
           'WGER_DOCKER_HOST': os.environ.get('WGER_DOCKER_HOST', f'unix://{Path.home()}/.colima/default/docker.sock'),
           'WGER_PUBLIC_URL': os.environ.get('WGER_PUBLIC_URL', 'https://gym.tailnet.invalid:8098')}
    machinery = Path(__file__).resolve().parents[1]
    # Prepare in isolation before backup: a digest/toolchain failure cannot alter the gym.
    home = Path.home().resolve()
    with tempfile.TemporaryDirectory(prefix='wger-product-', dir=staging_root(home)) as directory:
        candidate = Path(directory)
        try:
            resolved = candidate.resolve(strict=True)
            if candidate.is_symlink() or not resolved.is_dir() or home not in resolved.parents:
                raise ValueError('DEPLOY_MISSING: staging candidate must be a directory beneath home')
        except (OSError, RuntimeError) as error:
            raise ValueError('DEPLOY_MISSING: staging candidate unavailable beneath home') from error
        candidate = resolved
        for name in PRODUCT_FILES:
            original = source / PREFIX / name
            if original.is_symlink() or not original.is_file():
                raise ValueError('DEPLOY_MISSING: missing regular product file: ' + name)
            destination = candidate / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, destination)
        subprocess.run(['bash', str(candidate / 'patches/prepare-react.sh')], env=env, check=True)
        expected = hashlib.sha256((candidate / 'overrides/react-main.js.next').read_bytes()).hexdigest()
        from wger_preflight import run
        preflight = run(machinery, live, env)
        # A replayed UI revision must not downgrade independently installed proxy/sync fixes.
        for name in ('config/nginx.conf', 'config/powersync.yaml', 'config/sync_rules.yaml'):
            if PREFIX + name not in args.changed_file:
                shutil.copyfile(live / name, candidate / name)
        env.update(WGER_SOURCE_DEPLOY=str(candidate), WGER_EXPECTED_SHA256=expected,
                   WGER_RELEASE_COMMIT=args.commit)
        result = subprocess.run(['bash', str(machinery / PREFIX / 'patches/release-web.sh')],
                                env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError('release failed: ' + result.stderr[-6000:])
        proof = json.loads(result.stdout.splitlines()[-1])
        if proof.get('status') != 'deployed' or proof.get('commit') != args.commit or proof['live_proof']['normalized_sha256'] != expected:
            raise ValueError('release did not return matching live proof')
        proof['preflight'] = {key: preflight[key] for key in ('live_baseline_unchanged', 'disposable_resources_removed', 'media_sha256')}
        return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--changed-file', action='append', default=[])
    parser.add_argument('--deploy-root', default='~/fitness-wger')
    args = parser.parse_args()
    try:
        proof = deploy(args)
    except Exception as error:
        reason = failure_reason(error)
        print(json.dumps({'status': 'DEPLOY_MISSING', 'adapter': 'wger', 'commit': args.commit, 'reason': reason}))
        return 1
    print(json.dumps(proof))
    return 0


if __name__ == '__main__':
    sys.exit(main())
