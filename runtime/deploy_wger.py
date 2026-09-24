#!/usr/bin/env python3
"""Factory-only bridge from a merged source tree to the reviewed gym release."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

PREFIX = 'deployment/wger/'
# Explicit custody: never copy a directory recursively into the live deployment.
PRODUCT_FILES = (
    'compose.yaml', 'Dockerfile', 'settings-main.py', 'formats/en_AU/formats.py',
    'config/nginx.conf', 'config/powersync.yaml', 'config/sync_rules.yaml',
    'patches/prepare-react.sh', 'patches/patch_australian_dates.py',
    'patches/patch_australian_template_dates.py', 'patches/patch_australian_pdf.py',
    'patches/patch_progression_chart.py', 'patches/patch_muscle_diagram.py',
    'patches/patch_footer.py',
)
MACHINERY = ('patches/release-web.sh', 'patches/release_web.py', 'patches/release_env.py',
             'patches/setup-powersync-storage.py')
OPERATIONS = ('operations/backup.py', 'operations/snapshot.py', 'operations/restore-drill.py',
              'operations/cleanup-drill.py', 'operations/recovery-drill.py',
              'operations/cleanup-recovery.py', 'operations/com.cortana.fitness-wger.backup.plist',
              'com.cortana.fitness-wger.vm.plist')
EVIDENCE_FILES = ('.gitignore', 'issue-83-deployment-plan.txt', 'config/private.env.example',
                  'patches/test_release_route.py', 'patches/test_patch_progression_chart.py',
                  'patches/test_patch_ux_wave1.py', 'patches/test_patch_ux_wave3.py',
                  'operations/test_backup_route.py')


def normalize_bundle(data):
    return re.sub(rb'sourceMappingURL=main\.js\.[0-9a-f]{12}\.map',
                  b'sourceMappingURL=main.js.map', data)


def check_surfaces(paths):
    for path in paths:
        if not path.startswith(PREFIX):
            raise ValueError('DEPLOY_MISSING: non-gym surface ' + path)
        relative = path[len(PREFIX):]
        if relative not in PRODUCT_FILES + MACHINERY + EVIDENCE_FILES:
            # Operations/launchd/private environment require separately reviewed activation.
            kind = 'operations activation' if relative in OPERATIONS else 'unsupported surface'
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


def deploy(args):
    check_surfaces(args.changed_file)
    source = Path(args.source).resolve()
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if not re.fullmatch('[0-9a-f]{40}', args.commit) or revision != args.commit:
        raise ValueError('DEPLOY_MISSING: source HEAD does not match requested revision')
    if subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--', 'deployment/wger'], text=True).strip():
        raise ValueError('DEPLOY_MISSING: gym product source has uncommitted changes')
    live = Path(args.deploy_root).expanduser().resolve()
    if not (live / 'config/private.env').is_file():
        raise ValueError('DEPLOY_MISSING: existing private gym environment unavailable')
    env = {**os.environ, **existing_locks(), 'WGER_DEPLOY_DIR': str(live),
           'WGER_DOCKER_HOST': f'unix://{Path.home()}/.colima/default/docker.sock',
           'WGER_PUBLIC_URL': os.environ.get('WGER_PUBLIC_URL', 'https://gym.tailnet.invalid:8098')}
    machinery = Path(__file__).resolve().parents[1]
    # Build in isolation before backup: a digest/toolchain failure cannot alter the gym.
    with tempfile.TemporaryDirectory(prefix='wger-product-') as directory:
        candidate = Path(directory)
        for name in PRODUCT_FILES:
            original = source / PREFIX / name
            if original.is_symlink() or not original.is_file():
                raise ValueError('DEPLOY_MISSING: missing regular product file: ' + name)
            destination = candidate / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, destination)
        # Build the pinned fork before prepare-react extracts its upstream inputs.
        image = re.search(r'^IMAGE=(\S+)$', (candidate / 'patches/prepare-react.sh').read_text(), re.MULTILINE)
        if not image:
            raise ValueError('DEPLOY_MISSING: prepare-react image pin unavailable')
        subprocess.run(['docker', '-H', env['WGER_DOCKER_HOST'], 'build', '--tag', image[1], str(candidate)], env=env, check=True)
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


def failure_reason(error):
    # Subprocess exception text includes argv; stdout/stderr can contain configuration.
    if isinstance(error, subprocess.CalledProcessError):
        message = f'command failed with exit status {error.returncode}'
    elif isinstance(error, subprocess.TimeoutExpired):
        message = f'command timed out after {error.timeout} seconds'
    else:
        message = str(error)
    message = re.sub(r'[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"\'<>]+', '[REDACTED]', message)
    message = re.sub(r'(?i)\b(?:authorization\s*[:=]\s*)?(?:bearer|basic)\s+\S+', '[REDACTED]', message)
    message = re.sub(r'''(?ix)([\w-]*(?:password|passwd|secret|token|api[_-]?key|authorization)[\w-]*["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''', r'\1[REDACTED]', message)
    return f'{type(error).__name__}: ' + ' '.join(message.split())[:1000]


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
