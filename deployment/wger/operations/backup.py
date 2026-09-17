#!/usr/bin/env python3
"""Private full wger snapshots. No Git publishing, volume deletion or live restart."""
import argparse, datetime, fcntl, hashlib, json, os, pathlib, shlex, subprocess, sys
from snapshot import verify_snapshot, write_manifest

LIMA='/usr/local/bin/limactl'
VM='fitness-wger'


def vm(script, *, output=None, data=None, source_host=None):
    command=[LIMA,'shell',VM,'bash','-lc',script]
    if source_host:command=['/usr/bin/ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',source_host,shlex.join(command)]
    return subprocess.run(command,input=data,stdout=output or subprocess.PIPE,stderr=subprocess.PIPE,check=True).stdout


def sha256(path):
    digest=hashlib.sha256()
    with pathlib.Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def encrypt_snapshot(root, output, passphrase_file, *, runner=subprocess.run):
    root=pathlib.Path(root).resolve();output=pathlib.Path(output).expanduser().resolve()
    passphrase_file=pathlib.Path(passphrase_file).expanduser().resolve()
    manifest=verify_snapshot(root)
    if 'images.json' not in manifest['files']:raise ValueError('Migration snapshot requires image identities')
    if not passphrase_file.is_file() or passphrase_file.is_symlink() or passphrase_file.stat().st_mode&0o077:
        raise ValueError('Passphrase file must be a private regular file')
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if output.exists() or output.with_suffix(output.suffix+'.json').exists():raise FileExistsError(output)
    passphrase=passphrase_file.read_bytes()
    if len(passphrase.strip())<32:raise ValueError('Passphrase must contain at least 32 bytes')
    runner(['/usr/bin/hdiutil','create','-srcfolder',str(root),'-volname','fitness-wger-migration','-fs','APFS','-format','UDZO','-encryption','AES-256','-stdinpass',str(output)],input=passphrase,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True)
    output.chmod(0o600)
    receipt={'format':1,'encrypted_bundle':output.name,'encryption':'AES-256 encrypted disk image','sha256':sha256(output),'snapshot_manifest_sha256':sha256(root/'manifest.json'),'contains_private_health_data':True,'handling':'private migration artifact; never publish or commit'}
    receipt_path=output.with_suffix(output.suffix+'.json');receipt_path.write_text(json.dumps(receipt,indent=2)+'\n');receipt_path.chmod(0o600)
    return receipt_path


def snapshot(destination, *, source_host=None):
    os.umask(0o077)
    destination=pathlib.Path(destination).expanduser().resolve();destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=(destination/'.snapshot.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    root=destination/('wger-'+stamp);root.mkdir(mode=0o700)
    media_command="cd ~/fitness-wger && docker compose exec -T web sh -c 'cd /home/wger/media && find . -type f -exec sha256sum {} + | sort'"
    try:
        before=vm(media_command,source_host=source_host)
        with (root/'database.dump').open('wb') as f:
            vm("cd ~/fitness-wger && docker compose exec -T db sh -c 'pg_dump --format=custom --no-owner --no-acl -U \"$POSTGRES_USER\" \"$POSTGRES_DB\"'",output=f,source_host=source_host)
        with (root/'media.tar').open('wb') as f:
            vm("cd ~/fitness-wger && docker compose exec -T web tar -C /home/wger/media -cf - .",output=f,source_host=source_host)
        after=vm(media_command,source_host=source_host)
        if before!=after:raise RuntimeError('Media changed during snapshot; retained incomplete snapshot, retry at idle')
        (root/'media-sha256.txt').write_bytes(after)
        (root/'images.json').write_bytes(vm("cd ~/fitness-wger && docker compose images --format json",source_host=source_host))
        with (root/'deployment.tar').open('wb') as f:
            vm("cd ~/fitness-wger && tar -cf - compose.yaml config overrides",output=f,source_host=source_host)
        write_manifest(root,stamp)
        (destination/'latest.json').write_text(json.dumps({'snapshot':str(root),'created_at':stamp})+'\n')
        print(json.dumps({'snapshot':str(root),'snapshot_manifest_written':True,'size_bytes':sum(p.stat().st_size for p in root.iterdir())}))
    except Exception:
        (root/'INCOMPLETE').touch();raise
    return root


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--destination',default='~/Backups/fitness-coach/wger');parser.add_argument('--source-host');parser.add_argument('--encrypted-bundle');parser.add_argument('--passphrase-file');args=parser.parse_args()
    if bool(args.encrypted_bundle)!=bool(args.passphrase_file):parser.error('--encrypted-bundle and --passphrase-file are required together')
    try:
        root=snapshot(args.destination,source_host=args.source_host)
        if args.encrypted_bundle:print(json.dumps({'encrypted_receipt':str(encrypt_snapshot(root,args.encrypted_bundle,args.passphrase_file))}))
    except subprocess.CalledProcessError as e:
        print('Snapshot failed in guest command (details suppressed to protect configuration)',file=sys.stderr);sys.exit(e.returncode)
