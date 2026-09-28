"""Opt-in real PostgreSQL restore regression; never connects to the gym database."""
import ast
import os
from pathlib import Path
import subprocess
import unittest
import uuid


@unittest.skipUnless(os.environ.get('WGER_STORAGE_POSTGRES_TEST') == '1', 'requires disposable Docker PostgreSQL')
class PowerSyncStorageTest(unittest.TestCase):
    def test_restore_repairs_storage_without_changing_application_data(self):
        docker = ['docker', '--host', os.environ.get('WGER_DOCKER_HOST', 'unix://' + str(Path.home() / '.colima/default/docker.sock'))]
        name = 'powersync-grants-test-' + uuid.uuid4().hex[:12]
        def run(*args, data=None):
            return subprocess.run([*docker, *args], input=data, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, check=True).stdout
        def sql(statement):
            return run('exec', '-i', name, 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', 'fixture', '-At', data=statement.encode()).decode()
        run('run', '-d', '--name', name, '--network', 'none', '--tmpfs', '/var/lib/postgresql/data',
            '-e', 'POSTGRES_HOST_AUTH_METHOD=trust',
            'docker.io/postgres:15-alpine@sha256:fe0737ba566a2c5b2a28f34433c0a423261900ec17b9bf7ad115e1aae7e57f1b')
        try:
            run('exec', name, 'sh', '-c', 'for i in $(seq 1 60); do pg_isready -U postgres && exit 0; sleep 1; done; exit 1')
            run('exec', name, 'createdb', '-U', 'postgres', 'fixture')
            sql("CREATE ROLE storage LOGIN; CREATE SCHEMA powersync AUTHORIZATION storage; CREATE TABLE powersync.bucket(id serial PRIMARY KEY, value text); ALTER TABLE powersync.bucket OWNER TO storage; CREATE TABLE public.workout(id integer PRIMARY KEY, value text); INSERT INTO public.workout VALUES (1,'preserve athlete row'); INSERT INTO powersync.bucket(value) VALUES ('preserve sync row');")
            dump = run('exec', name, 'pg_dump', '-Fc', '-U', 'postgres', 'fixture')
            run('exec', name, 'dropdb', '-U', 'postgres', 'fixture')
            run('exec', name, 'createdb', '-U', 'postgres', 'fixture')
            run('exec', '-i', name, 'pg_restore', '--exit-on-error', '--no-owner', '--no-acl', '-U', 'postgres', '-d', 'fixture', data=dump)
            with self.assertRaises(subprocess.CalledProcessError) as denied:
                sql("SET ROLE storage; CREATE SCHEMA IF NOT EXISTS powersync; SELECT * FROM powersync.bucket;")
            self.assertIn(b'permission denied for database fixture', denied.exception.stderr)
            tree = ast.parse(Path(__file__).with_name('setup-powersync-storage.py').read_text())
            bootstrap = next(node.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'execute')
            for value in ('storage', 'disposable-password', 'powersync'):
                bootstrap = bootstrap.replace('%s', "'" + value + "'", 1)
            bootstrap = bootstrap.replace('%%', '%')
            sql(bootstrap)
            sql(bootstrap)  # The same initializer is safe after a repeated rollback.
            result = sql("SET ROLE storage; CREATE SCHEMA IF NOT EXISTS powersync; SELECT value FROM powersync.bucket; INSERT INTO powersync.bucket(value) VALUES ('after restore'); RESET ROLE; SELECT value FROM public.workout; SELECT tableowner FROM pg_tables WHERE schemaname='public' AND tablename='workout'; SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname='storage';")
            self.assertIn('preserve sync row', result)
            self.assertIn('preserve athlete row', result)
            self.assertIn('postgres', result)
            self.assertEqual(result.splitlines()[-1], 'f')
        finally:
            run('rm', '-f', name)


if __name__ == '__main__':
    unittest.main()
