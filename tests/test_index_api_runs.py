"""Gzip transfers use a real local HTTP server; no provider or cloud calls."""
import gzip
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from packs.indexing.modal import api_runs


class TransferTest(unittest.TestCase):
    def test_upload_and_download_round_trip(self):
        raw = b'name,profile\nJordan Bravo,"{}"\n' * 10000
        uploaded = []
        requests = []
        output = [gzip.compress(raw)]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'upload_url': base + '/input', 'object': body['name']}).encode())

            def do_PUT(self):
                uploaded.append(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(200)
                self.end_headers()

            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(output[0])

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        base = f'http://127.0.0.1:{server.server_port}'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory, patch.object(api_runs, '_base', return_value=base), \
                    patch.object(api_runs, '_token', return_value='test'):
                source = Path(directory) / 'people.csv'
                source.write_bytes(raw)
                self.assertEqual(api_runs.upload(source, 'people.csv'), 'people.csv.gz')
                self.assertEqual(requests[0], {'name': 'people.csv.gz'})
                self.assertEqual(gzip.decompress(uploaded[0]), raw)
                target = Path(directory) / 'local-search.duckdb'
                target.write_bytes(b'previous index')
                self.assertEqual(api_runs.download('index', target.name, target), len(raw))
                self.assertEqual(target.read_bytes(), raw)
                self.assertEqual(target.with_name(target.name + '.bkup').read_bytes(), b'previous index')
                self.assertEqual(requests[-1], '/runs/index/files/local-search.duckdb.gz')
                output[0] = output[0][:-5]
                with self.assertRaises(EOFError):
                    api_runs.download('index', target.name, target)
                self.assertEqual(target.read_bytes(), raw)
                self.assertEqual(target.with_name(target.name + '.bkup').read_bytes(), b'previous index')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
