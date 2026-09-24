#!/usr/bin/env python3
"""Restore a verified private snapshot to a separate disposable Docker project.

Only run in the fitness-wger VM. Never addresses production containers/volumes.
The drill uses a new named network and volumes and does not start workers or sync.
"""
import argparse,hashlib,json,os,pathlib,secrets,subprocess,tarfile,time,uuid
DOCKER_HOST=f'unix://{pathlib.Path.home()}/.colima/docker.sock'
DOCKER=('docker','-H',DOCKER_HOST)
PG='docker.io/postgres:15-alpine@sha256:fe0737ba566a2c5b2a28f34433c0a423261900ec17b9bf7ad115e1aae7e57f1b'
WEB='ghcr.io/cubatica/fitness-wger:135d8569a3eb27c9f0f74e865d56372421a61294'
NGINX='docker.io/nginx:1.28-alpine@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236'
def run(*a,data=None):return subprocess.run([*DOCKER,*a],input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True).stdout
def wait_for_database(name, *, attempts=60, delay=1):
    for _ in range(attempts):
        try:
            run('exec',name+'-db','pg_isready','-h','127.0.0.1','-U','restore','-d','wger')
            return
        except subprocess.CalledProcessError:
            time.sleep(delay)
    raise RuntimeError('disposable database did not become ready')
def web_override_mounts(work):
    mounts = (
        ('overrides/react-main.js', '/home/wger/src/node_modules/@wger-project/react-components/build/main.js'),
        ('overrides/template.html', '/home/wger/src/wger/core/templates/template.html'),
        ('overrides/history-overview.html', '/home/wger/src/wger/exercises/templates/history/overview.html'),
        ('overrides/api-key.html', '/home/wger/src/wger/core/templates/user/api_key.html'),
        ('overrides/pdf.py', '/home/wger/src/wger/utils/pdf.py'),
        ('settings-main.py', '/home/wger/src/settings/main.py'),
        ('formats/en_AU/formats.py', '/home/wger/src/wger/formats/en_AU/formats.py'),
    )
    return tuple(str(work / source) + ':' + target + ':ro' for source, target in mounts)




def main():
    os.umask(0o077)
    ap=argparse.ArgumentParser();ap.add_argument('snapshot');ap.add_argument('--port',type=int,default=18197);args=ap.parse_args()
    src=pathlib.Path(args.snapshot).resolve();manifest=json.loads((src/'manifest.json').read_text())
    if (src/'INCOMPLETE').exists():raise ValueError('Incomplete snapshot')
    for name,wanted in manifest['files'].items():
        p=src/name
        if p.parent!=src or hashlib.sha256(p.read_bytes()).hexdigest()!=wanted:raise ValueError('Snapshot checksum mismatch')
    name='wger-restore-'+uuid.uuid4().hex[:10];work=src.parent/name;work.mkdir(mode=0o700)
    with tarfile.open(src/'deployment.tar') as t:t.extractall(work,filter='data')
    config={}
    for line in (work/'config/private.env').read_text().splitlines():
        if line and not line.startswith('#') and '=' in line:
            k,v=line.split('=',1);config[k]=v
    password=secrets.token_urlsafe(36)
    config.update(POSTGRES_USER='restore',POSTGRES_PASSWORD=password,POSTGRES_DB='wger',DJANGO_DB_USER='restore',DJANGO_DB_PASSWORD=password,DJANGO_DB_DATABASE='wger',DJANGO_DB_HOST=name+'-db',DJANGO_DB_PORT='5432',DJANGO_CACHE_BACKEND='django.core.cache.backends.locmem.LocMemCache',DJANGO_CACHE_LOCATION='restore-only',USE_CELERY='False',ENABLE_EMAIL='False',DJANGO_PERFORM_MIGRATIONS='True',SYNC_EXERCISES_ON_STARTUP='False',SYNC_EXERCISE_IMAGES_CELERY='False',SYNC_EXERCISES_CELERY='False',SYNC_INGREDIENTS_CELERY='False',SITE_URL=f'http://127.0.0.1:{args.port}',STATIC_URL='/static/',MEDIA_URL='/media/',CSRF_TRUSTED_ORIGINS=f'http://127.0.0.1:{args.port}',DJANGO_CLEAR_STATIC_FIRST='False',DJANGO_DEBUG='False')
    for key in list(config):
        if key.startswith('PS_') or key.startswith('JWT_') or key in ['CELERY_BROKER','CELERY_BACKEND']:config.pop(key)
    env=work/'restore.env';env.write_text('\n'.join(k+'='+v for k,v in config.items())+'\n')
    labels=['--label','fitness.backup.restore-drill='+name]
    run('network','create',*labels,'--internal',name)
    run('network','create',*labels,name+'-front')
    for volume in ['db','media','static']:run('volume','create',*labels,name+'-'+volume)
    receipt={'snapshot':str(src),'project':name,'port':args.port,'created_containers':[name+'-'+n for n in ['db','web','nginx']],'volumes':[name+'-'+n for n in ['db','media','static']],'network':name,'networks':[name,name+'-front'],'live_project_untouched':True,'state':'starting'}
    path=work/'receipt.json';path.write_text(json.dumps(receipt,indent=2))
    run('run','-d','--name',name+'-db',*labels,'--network',name,'--memory','256m','--cpus','0.25','--env-file',str(env),'-v',name+'-db:/var/lib/postgresql/data',PG)
    wait_for_database(name)
    run('exec','-i',name+'-db','pg_restore','--exit-on-error','--no-owner','--no-acl','-U','restore','-d','wger',data=(src/'database.dump').read_bytes())
    run('run','--rm','-i',*labels,'--network','none','--memory','128m','--cpus','0.25','--entrypoint','tar','-v',name+'-media:/home/wger/media',WEB,'-C','/home/wger/media','-xf','-',data=(src/'media.tar').read_bytes())
    override_mounts=sum((('-v',mount) for mount in web_override_mounts(work)),())
    run('run','-d','--name',name+'-web',*labels,'--network',name,'--memory','512m','--cpus','0.5','--env-file',str(env),'-v',name+'-media:/home/wger/media','-v',name+'-static:/home/wger/static',*override_mounts,'--entrypoint','/bin/sh',WEB,'-c','python3 manage.py migrate --no-input >/tmp/restore-migrate.log 2>&1 && python3 manage.py collectstatic --no-input >/tmp/restore-static.log 2>&1 && gunicorn wger.wsgi:application --workers 1 --bind 0.0.0.0:8000')
    nginx=work/'restore-nginx.conf';nginx.write_text('server { listen 80; location / { proxy_pass http://'+name+'-web:8000; proxy_set_header Host $http_host; proxy_set_header X-Forwarded-Proto http; } location /static/ { alias /wger/static/; } location /media/ { alias /wger/media/; } }\n')
    run('run','-d','--name',name+'-nginx',*labels,'--network',name+'-front','--network',name,'--memory','64m','--cpus','0.25','-p',f'127.0.0.1:{args.port}:80','-v',str(nginx)+':/etc/nginx/conf.d/default.conf:ro','-v',name+'-media:/wger/media:ro','-v',name+'-static:/wger/static:ro',NGINX)
    receipt['state']='restored-awaiting-independent-application-check';path.write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps({'receipt':str(path),'project':name,'port':args.port}))
if __name__=='__main__':main()
