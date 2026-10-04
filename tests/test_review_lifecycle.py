"""A cold install keeps one owned server through dependency setup and review."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'packs.ingestion.primitives.deep_context.review.reconcile_review_web'


class ReviewLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.base = f'http://127.0.0.1:{self.port}'
        self.env = {**os.environ, 'PYTHONPATH': str(ROOT)}

    def start(self):
        return subprocess.run([sys.executable, '-S', '-m', MODULE, 'start', '--stage', 'install',
                               '--port', str(self.port)], cwd=self.root, env=self.env,
                              capture_output=True, text=True, timeout=12)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=3) as response:
            return response.status, response.read()

    def stop(self, pid):
        os.kill(pid, 15)

    def test_cold_install_serves_before_dependencies_and_reuses_same_process(self):
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        started = json.loads(result.stdout)
        self.addCleanup(self.stop, started['pid'])
        self.assertEqual(started['url'], self.base + '/install')
        health = json.loads(self.get('/healthz')[1])
        self.assertEqual(health['repo_root'], str(self.root))
        self.assertEqual(health['pid'], started['pid'])
        self.assertIn(b'/app/assets/app.js', self.get('/install')[1])
        self.assertTrue(self.get('/app/assets/app.js')[1])
        self.assertEqual(self.get('/api/install')[0], 200)
        reused = self.start()
        self.assertEqual(reused.returncode, 0, reused.stderr)
        self.assertEqual(json.loads(reused.stdout)['pid'], started['pid'])
        self.assertEqual(json.loads(reused.stdout)['status'], 'reused')
        self.assertEqual(json.loads(self.get('/api/status')[1])['primitive'], 'reconcile_review_web')

    def test_start_refuses_foreign_listener_without_stopping_it(self):
        class Foreign(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({'primitive': 'reconcile_review_web', 'repo_root': '/another/repo', 'pid': os.getpid()}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', self.port), Foreign)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        result = self.start()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('another', result.stderr.lower())
        self.assertEqual(self.get('/healthz')[0], 200)

    def test_missing_dependencies_leave_install_page_available(self):
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(self.stop, json.loads(result.stdout)['pid'])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.get('/accounts/api/accounts')
        self.assertEqual(error.exception.code, 503)
        self.assertIn('bin/setup-python', error.exception.read().decode())
        error.exception.close()
        self.assertEqual(self.get('/install')[0], 200)

    def test_cold_server_accepts_source_choices_after_dependencies_arrive(self):
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(self.stop, json.loads(result.stdout)['pid'])
        (self.root / '.venv').symlink_to(ROOT / '.venv', target_is_directory=True)
        request = urllib.request.Request(self.base + '/api/install/sources',
                                         data=json.dumps({'sources': [], 'skip': True}).encode(),
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 202)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = json.loads(self.get('/api/install')[1])
            if status['status'] in {'completed', 'failed'}:
                break
            time.sleep(0.05)
        self.assertEqual((status['status'], status['step']), ('completed', 'ready'), status)

    def test_unsupported_store_reports_recovery_without_breaking_install_page(self):
        import sqlite3

        path = self.root / '.powerpacks/deep-context/deep-context.sqlite'
        path.parent.mkdir(parents=True)
        sqlite3.connect(path).close()
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        pid = json.loads(result.stdout)['pid']
        self.addCleanup(self.stop, pid)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.get('/api/status')
        self.assertEqual(error.exception.code, 503)
        payload = json.loads(error.exception.read())
        error.exception.close()
        self.assertEqual(payload['retry_command'], 'bin/deep-context ensure-parents')
        self.assertIn('Cannot read Deep Context database', payload['error'])
        self.assertEqual(self.get('/install')[0], 200)
        self.assertEqual(json.loads(self.get('/healthz')[1])['pid'], pid)

    def test_empty_store_becomes_review_in_same_process(self):
        from packs.ingestion.primitives.deep_context.db.store import Db

        path = self.root / '.powerpacks/deep-context/deep-context.sqlite'
        Db(path)
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        pid = json.loads(result.stdout)['pid']
        self.addCleanup(self.stop, pid)
        self.assertEqual(json.loads(self.get('/api/status')[1])['stage'], 'install')
        with urllib.request.urlopen(self.base + '/', timeout=3) as response:
            self.assertEqual(response.url, self.base + '/searches')
        from packs.powerset.primitives.install.status import InstallState, InstallStatus, InstallStep
        InstallStatus(self.root).write(step=InstallStep.READY, status=InstallState.COMPLETED,
                                       message='Powerpacks is installed.', pid=os.getpid())
        with urllib.request.urlopen(self.base + '/', timeout=3) as response:
            self.assertEqual(response.url, self.base + '/install')
        (self.root / '.venv').symlink_to(ROOT / '.venv', target_is_directory=True)
        import sqlite3
        with sqlite3.connect(path) as connection:
            connection.execute("INSERT INTO parents(parent_id,public_identifier,display_name) VALUES(?,?,?)",
                               ('parent-jordan-bravo', 'jordan-bravo', 'Jordan Bravo'))
        status = json.loads(self.get('/api/status')[1])
        self.assertEqual(status['primitive'], 'reconcile_review_web')
        self.assertNotEqual(status['stage'], 'install')
        self.assertEqual(json.loads(self.get('/healthz')[1])['pid'], pid)
        self.assertEqual(self.get('/install')[0], 200)

    def test_realization_keeps_same_server_and_exports_reviewed_roster(self):
        from packs.ingestion.primitives.deep_context.db.store import Db
        from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents
        from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS, generate_person_id
        from packs.shared.csv_io import CsvIO

        directory = self.root / '.powerpacks/network-import/merged'
        people_csv = directory / 'people.csv'
        rows = [{column: '' for column in PEOPLE_SCHEMA_COLUMNS} for _ in range(2)]
        for row, (slug, name) in zip(rows, [('jordan-bravo', 'Jordan Bravo'), ('casey-delta', 'Casey Delta')]):
            row.update(id=generate_person_id(slug), public_identifier=slug,
                       linkedin_url=f'https://www.linkedin.com/in/{slug}', full_name=name,
                       source_channels='linkedin')
        CsvIO.write_dict_rows(people_csv, PEOPLE_SCHEMA_COLUMNS, rows)
        db = Db(self.root / '.powerpacks/deep-context/deep-context.sqlite')
        EnsureParents(db=db, people_csv=people_csv).run()
        for slug in ('jordan-bravo', 'casey-delta'):
            db.decide_identity(slug, 'verify')
        (self.root / '.venv').symlink_to(ROOT / '.venv', target_is_directory=True)
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        pid = json.loads(result.stdout)['pid']
        self.addCleanup(self.stop, pid)
        before = json.loads(self.get('/api/status')[1])
        self.assertEqual(before['next_action'], 'realize')
        action = subprocess.run([sys.executable, '-m', MODULE, 'status'], cwd=self.root,
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(action.returncode, 0, action.stderr)
        self.assertEqual(json.loads(action.stdout)['command'], 'bin/deep-context realize')
        exported = subprocess.run([sys.executable, '-m',
                                  'packs.ingestion.primitives.deep_context.realize.export_people'],
                                 cwd=self.root, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        payload = json.loads(exported.stdout)
        self.assertEqual((payload['status'], payload['rows']), ('completed', len(rows)))
        self.assertEqual(len(CsvIO.read_dict_rows(people_csv)), len(rows))
        self.assertEqual(json.loads(self.get('/healthz')[1])['pid'], pid)
        self.assertEqual(json.loads(self.get('/api/status')[1])['next_action'], 'realize')
        self.assertEqual(self.get('/install')[0], 200)


if __name__ == '__main__':
    unittest.main()
