#!/usr/bin/env python3
"""Local web app with a durable SMTP sending worker: python3 serve.py."""
import json
import os
import sys
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from outreach import MailQueue, load_env
from catalog_jobs import CatalogJobs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'api'))
import yc


class Handler(SimpleHTTPRequestHandler):
    queue = None
    catalog = None

    def end_headers(self):
        if urllib.parse.urlparse(self.path).path in ('/', '/index.html'):
            self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def json_response(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def local_request(self):
        host = self.headers.get('Host', '')
        expected = {f'localhost:{self.server.server_port}', f'127.0.0.1:{self.server.server_port}'}
        if host not in expected:
            return False
        origin = self.headers.get('Origin')
        return not origin or origin == f'http://{host}'

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if not self.local_request():
            return self.json_response(403, {'error': 'Local requests only.'})
        if path == '/api/mail':
            return self.json_response(200, self.queue.status())
        if path == '/api/catalog':
            return self.json_response(200, self.catalog.status()) if self.catalog else self.json_response(503, {'error':'Company refresh service unavailable.'})
        if path == '/api/yc':
            return yc.handler.do_GET(self)
        # Never expose .env, the queue database, or repository files via the static server.
        if path not in ('/', '/index.html', '/vendor/tabulator/tabulator.min.js', '/vendor/tabulator/tabulator.min.css'):
            return self.send_error(404)
        return super().do_GET()

    def do_HEAD(self):
        if not self.local_request() or urllib.parse.urlparse(self.path).path not in ('/', '/index.html'):
            return self.send_error(404)
        return super().do_HEAD()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ('/api/mail', '/api/catalog'):
            return self.send_error(404)
        if not self.local_request() or self.headers.get('X-Outreach-Token') != self.queue.token:
            return self.json_response(403, {'error': 'Refresh this page before changing the queue.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 2_500_000:
                raise ValueError('Invalid request size.')
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('Expected JSON.')
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError('Expected an object.')
            if path == '/api/catalog':
                if payload.get('action') != 'refresh' or not self.catalog:
                    raise ValueError('Company refresh service unavailable or invalid action.')
                result = self.catalog.start()
            else:
                result = self.queue.action(payload)
            return self.json_response(200, result)
        except (ValueError, TypeError) as error:
            return self.json_response(400, {'error': str(error)})


if __name__ == '__main__':
    os.chdir(HERE)
    load_env(HERE / '.env')
    server = ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('PORT', 8765))), Handler)
    marker = HERE / '.outreach' / 'service-data-path'
    data_dir = Path(os.environ.get('OUTREACH_DATA_DIR') or (marker.read_text().strip() if marker.exists() else str(HERE / '.outreach')))
    Handler.queue = MailQueue(data_dir / 'queue.sqlite3')
    Handler.catalog = CatalogJobs(data_dir, HERE / 'data' / 'yc_companies.json', yc.batches, yc.companies)
    yc.CATALOG_PATH = Handler.catalog.path
    Handler.queue.start_worker()
    print(f'http://localhost:{server.server_port} — auto-send queue starts paused')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        Handler.queue.stop_event.set()
        server.server_close()
