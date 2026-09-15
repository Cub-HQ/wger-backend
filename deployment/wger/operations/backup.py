#!/usr/bin/env python3
"""Private full wger snapshots. No Git publishing, volume deletion or live restart."""
import argparse, datetime, fcntl, json, os, pathlib, subprocess, sys
from snapshot import write_manifest
LIMA='/usr/local/bin/limactl'
VM='fitness-wger'
BASE='fitness-wger'

def vm(script, *, output=None, data=None):
    return subprocess.run([LIMA,'shell',VM,'bash','-lc',script],input=data,stdout=output or subprocess.PIPE,stderr=subprocess.PIPE,check=True).stdout

def snapshot(destination):
    os.umask(0o077)
    destination=pathlib.Path(destination).expanduser().resolve();destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=(destination/'.snapshot.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    root=destination/('wger-'+stamp);root.mkdir(mode=0o700)
    media_command="cd ~/fitness-wger && docker compose exec -T web sh -c 'cd /home/wger/media && find . -type f -exec sha256sum {} + | sort'"
    try:
        before=vm(media_command)
        with (root/'database.dump').open('wb') as f:
            vm("cd ~/fitness-wger && docker compose exec -T db sh -c 'pg_dump --format=custom --no-owner --no-acl -U \"$POSTGRES_USER\" \"$POSTGRES_DB\"'",output=f)
        with (root/'media.tar').open('wb') as f:
            vm("cd ~/fitness-wger && docker compose exec -T web tar -C /home/wger/media -cf - .",output=f)
        with (root/'powersync.dump').open('wb') as f:
            vm("cd ~/fitness-wger && docker compose exec -T db sh -c 'pg_dump --format=custom --no-owner --no-acl -U \"$POSTGRES_USER\" -n powersync \"$POSTGRES_DB\"'",output=f)
        after=vm(media_command)
        if before!=after:raise RuntimeError('Media changed during snapshot; retained incomplete snapshot, retry at idle')
        (root/'media-sha256.txt').write_bytes(after)
        with (root/'deployment.tar').open('wb') as f:
            vm("cd ~/fitness-wger && tar -cf - compose.yaml config overrides",output=f)
        write_manifest(root,stamp)
        (destination/'latest.json').write_text(json.dumps({'snapshot':str(root),'created_at':stamp})+'\n')
        print(json.dumps({'snapshot':str(root),'snapshot_manifest_written':True,'size_bytes':sum(p.stat().st_size for p in root.iterdir())}))
    except Exception:
        (root/'INCOMPLETE').touch();raise
    return root

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--destination',default='~/Backups/fitness-coach/wger');args=parser.parse_args()
    try:snapshot(args.destination)
    except subprocess.CalledProcessError as e:
        print('Snapshot failed in guest command (details suppressed to protect configuration)',file=sys.stderr);sys.exit(e.returncode)
