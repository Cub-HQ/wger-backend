#!/usr/bin/env python3
"""Private full wger snapshots from the reviewed local Docker runtime."""
import argparse, datetime, fcntl, hashlib, json, os, pathlib, stat, subprocess, sys
from snapshot import verify_snapshot, write_manifest

DEPLOY=pathlib.Path.home()/'fitness-wger'
WRITERS=('powersync','web','celery_worker','celery_beat')

def docker_host():
    host = os.environ.get('WGER_DOCKER_HOST', f'unix://{pathlib.Path.home()}/.colima/default/docker.sock')
    if not host.startswith('unix://') or not pathlib.Path(host[7:]).is_absolute():
        raise RuntimeError('Docker endpoint must be unix:// with an absolute socket path')
    if not pathlib.Path(host[7:]).is_socket():
        raise RuntimeError(f'Docker socket is unavailable: {host}')
    return host


def docker(*args, output=None, check=True):
    return subprocess.run(['docker','-H',docker_host(),*args],stdout=output or subprocess.PIPE,stderr=subprocess.PIPE,check=check).stdout


def compose(*args, output=None, check=True):
    return docker('compose','-f',str(DEPLOY/'compose.yaml'),*args,output=output,check=check)

def inspect_state(container):
    records=json.loads(docker('inspect',container).decode())
    if len(records)!=1:raise RuntimeError(f'expected one writer container inspection: {container}')
    record=records[0];state=record.get('State')
    if not isinstance(state,dict) or not record.get('Image') or not state.get('Status'):
        raise RuntimeError(f'incomplete writer container inspection: {container}')
    health=state.get('Health');health_status=health.get('Status') if isinstance(health,dict) else None
    return {'image':record['Image'],'status':state['Status'],'health':health_status}


def wait_for_state(container, expected, attempts=60):
    for _ in range(attempts):
        if inspect_state(container)==expected:return
        subprocess.run(['/bin/sleep','1'],check=True)
    raise RuntimeError(f'container did not restore prior state: {container}')


def acquire_custody(writer_lock,history_lock):
    if not writer_lock or not history_lock:raise RuntimeError('WGER_WRITER_LOCK and WGER_HISTORY_LOCK are required')
    custody=[]
    for path,namespace in [(pathlib.Path(writer_lock),'flock'),(pathlib.Path(history_lock),'lockf')]:
        if not path.is_file():raise RuntimeError(f'required writer lock is missing: {path}')
        handle=path.open('a');operation=fcntl.LOCK_EX|fcntl.LOCK_NB
        try:fcntl.flock(handle,operation) if namespace=='flock' else fcntl.lockf(handle,operation)
        except BlockingIOError:raise RuntimeError(f'writer/import custody is active: {path}') from None
        custody.append(handle)
    return custody

def private_destination(requested):
    home=pathlib.Path.home();approved=home/'fitness-coach-migration'
    destination=pathlib.Path(requested).expanduser()
    if not destination.is_absolute():destination=pathlib.Path.cwd()/destination
    if destination!=approved:raise ValueError('destination must be established private ~/fitness-coach-migration')
    current=pathlib.Path(destination.anchor)
    for part in home.parts[1:]:
        current=current/part
        state=current.lstat()
        if stat.S_ISLNK(state.st_mode):
            if state.st_uid==os.getuid():raise ValueError(f'destination ancestor must be a real directory: {current}')
        elif not stat.S_ISDIR(state.st_mode):
            raise ValueError(f'destination ancestor must be a directory: {current}')
    home_state=home.lstat();home_mode=stat.S_IMODE(home_state.st_mode)
    if home_state.st_uid!=os.getuid() or home_mode&0o022:
        raise ValueError('home directory must be owner-controlled')
    try:destination_state=destination.lstat()
    except FileNotFoundError:
        destination.mkdir(mode=0o700)
        destination_state=destination.lstat()
    if stat.S_ISLNK(destination_state.st_mode) or not stat.S_ISDIR(destination_state.st_mode) or destination_state.st_uid!=os.getuid() or stat.S_IMODE(destination_state.st_mode)!=0o700:
        raise ValueError('destination must be an owner-owned private real directory')
    return destination

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


def media_inventory():
    return docker('run','--rm','-v','fitness-wger_media:/media:ro','alpine:3.22','sh','-c','cd /media && find . -type f -exec sha256sum {} + | sort')

def deployment_services(work, docker_command=None):
    docker_command = ('docker', '-H', docker_host()) if docker_command is None else docker_command
    work = pathlib.Path(work).absolute()
    deployment_file(work, 'compose.yaml')
    deployment_file(work, 'config/private.env')
    result = subprocess.run([*docker_command, 'compose', '--project-directory', str(work),
                             '-f', str(work / 'compose.yaml'), 'config', '--format', 'json'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    services = json.loads(result.stdout)['services']
    for service in services.values():
        if not isinstance(service.get('image'), str) or not service['image']:
            raise RuntimeError('snapshot compose service requires an image')
    return services


def deployment_file(work, source):
    work = pathlib.Path(work).absolute()
    path = pathlib.Path(source)
    if not path.is_absolute():
        path = work / path
    if '..' in path.parts or not path.is_relative_to(work):
        raise RuntimeError(f'deployment bind source escapes deployment: {path}')
    for ancestor in (work, *(work / parent for parent in path.relative_to(work).parents if parent != pathlib.Path('.'))):
        if ancestor.is_symlink() or not ancestor.is_dir():
            raise RuntimeError(f'deployment bind source ancestor has wrong type: {ancestor}')
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f'deployment bind source has wrong type: {path}')
    if not path.is_file():
        raise RuntimeError(f'required deployment bind source is missing: {path}')
    return path


def service_bind_mounts(work, service):
    mounts = []
    for volume in service.get('volumes', []):
        if volume['type'] != 'bind':
            continue
        source = deployment_file(work, volume['source'])
        target = volume['target']
        if not target.startswith('/') or ':' in target or ':' in str(source):
            raise RuntimeError('unsupported deployment bind path')
        mounts.append(f'{source}:{target}' + (':ro' if volume.get('read_only') else ':rw'))
    return tuple(mounts)

def write_deployment_archive(path, *, deploy=None):
    deploy = DEPLOY if deploy is None else pathlib.Path(deploy)
    required = ('compose.yaml', 'config', 'overrides')
    optional = ('settings-main.py', 'formats/en_AU/formats.py')
    members = list(required)
    for name in optional:
        source = deploy / name
        if source.exists() or source.is_symlink():
            deployment_file(deploy, name)
        if source.is_file():
            members.append(name)
    for name in required:
        source = deploy / name
        if source.is_symlink() or (not source.is_file() if name == 'compose.yaml' else not source.is_dir()):
            raise RuntimeError(f'deployment archive member has wrong type: {source}')
        for child in source.rglob('*') if source.is_dir() else ():
            if child.is_symlink() or not (child.is_file() or child.is_dir()):
                raise RuntimeError(f'deployment archive member has wrong type: {child}')
    with pathlib.Path(path).open('wb') as output:
        subprocess.run([
            '/usr/bin/tar', '-C', str(deploy), '-cf', '-', *members,
        ], stdout=output, stderr=subprocess.PIPE, check=True)




def snapshot(destination):
    os.umask(0o077)
    destination=private_destination(destination)
    docker_host()
    if not (DEPLOY/'compose.yaml').is_file():
        raise RuntimeError('reviewed local gym deployment is unavailable')
    lock=(destination/'.snapshot.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    custody=acquire_custody(os.environ.get('WGER_WRITER_LOCK'),os.environ.get('WGER_HISTORY_LOCK'))
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    root=destination/('wger-'+stamp);root.mkdir(mode=0o700)
    writer_ids={};writer_states={};running_ids=[];capture_complete=False;primary_error=None
    try:
        writer_ids={service:compose('ps','-a','-q',service).decode().strip() for service in WRITERS}
        if any(not ident for ident in writer_ids.values()):raise RuntimeError('all gym writer containers must exist before snapshot')
        writer_states={service:inspect_state(ident) for service,ident in writer_ids.items()}
        running_ids=[writer_ids[service] for service,state in writer_states.items() if state['status']=='running']
        if running_ids:docker('stop',*running_ids)
        before=media_inventory()
        with (root/'database.dump').open('wb') as output:
            compose('exec','-T','db','sh','-c','pg_dump --format=custom --no-owner --no-acl -U "$POSTGRES_USER" "$POSTGRES_DB"',output=output)
        with (root/'media.tar').open('wb') as output:
            docker('run','--rm','-v','fitness-wger_media:/media:ro','alpine:3.22','tar','-C','/media','-cf','-','.',output=output)
        after=media_inventory()
        if before!=after:raise RuntimeError('Media changed inside closed writer snapshot window')
        (root/'media-sha256.txt').write_bytes(after)
        (root/'images.json').write_text(json.dumps(writer_states,indent=2)+'\n')
        write_deployment_archive(root/'deployment.tar')
        write_manifest(root,stamp);capture_complete=True
    except Exception as error:
        primary_error=error;(root/'INCOMPLETE').touch()
    finally:
        resume_error=None
        try:
            if running_ids:docker('start',*running_ids,check=False)
            if writer_states:
                for service,ident in writer_ids.items():wait_for_state(ident,writer_states[service])
                resumed={service:inspect_state(ident) for service,ident in writer_ids.items()}
                if resumed != writer_states:raise RuntimeError('snapshot did not restore exact gym writer container/image/state/health')
        except Exception as error:
            resume_error=error;(root/'INCOMPLETE').touch()
        finally:
            try:
                if writer_ids.get('web') in running_ids:
                    # A different writer's failed health check must not strand the proxy.
                    compose('exec','-T','nginx','nginx','-t')
                    compose('exec','-T','nginx','nginx','-s','reload')
            except Exception as error:
                (root/'INCOMPLETE').touch()
                if resume_error is not None:resume_error.add_note(f'nginx recovery also failed: {error}')
                else:resume_error=error
            for handle in custody:handle.close()
            lock.close()
        if primary_error is not None:
            if resume_error is not None:primary_error.add_note(f'writer recovery also failed: {resume_error}')
            raise primary_error
        if resume_error is not None:raise resume_error
    if not capture_complete:raise RuntimeError('snapshot capture did not complete')
    (destination/'latest.json').write_text(json.dumps({'snapshot':str(root),'created_at':stamp})+'\n')
    print(json.dumps({'snapshot':str(root),'snapshot_manifest_written':True,'size_bytes':sum(path.stat().st_size for path in root.iterdir())}))
    return root


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--destination',default='~/fitness-coach-migration');parser.add_argument('--encrypted-bundle');parser.add_argument('--passphrase-file');args=parser.parse_args()
    if bool(args.encrypted_bundle)!=bool(args.passphrase_file):parser.error('--encrypted-bundle and --passphrase-file are required together')
    try:
        root=snapshot(args.destination)
        if args.encrypted_bundle:print(json.dumps({'encrypted_receipt':str(encrypt_snapshot(root,args.encrypted_bundle,args.passphrase_file))}))
    except subprocess.CalledProcessError as error:
        print('Snapshot failed in local Docker command (details suppressed to protect configuration)',file=sys.stderr);sys.exit(error.returncode)
