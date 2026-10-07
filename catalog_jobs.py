"""Persistent YC directory refresh jobs, independent of browser and mail queue."""
import json
import threading
import time
import uuid
from pathlib import Path

class CatalogJobs:
    def __init__(self, folder, seed, fetch_batches, fetch_companies):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / 'company_catalog.json'
        self.state_path = self.folder / 'catalog_job.json'
        self.fetch_batches, self.fetch_companies = fetch_batches, fetch_companies
        self.lock = threading.RLock()
        self.thread = None
        if not self.path.exists():
            self._write(self.path, json.loads(Path(seed).read_text()))
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {'running':False, 'completed':0, 'total':0, 'failed':[], 'new_slugs':[]}
        if self.state.get('running'):
            self._launch()

    @staticmethod
    def _write(path, value):
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False))
        temporary.replace(path)

    def _checkpoint(self):
        self.state['updated_at'] = time.time()
        self._write(self.state_path, self.state)

    def status(self):
        with self.lock:
            return {key:value for key,value in self.state.items() if key not in ('remaining','baseline')}

    def _launch(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name='yc-catalog-refresh')
        self.thread.start()

    def start(self):
        with self.lock:
            if self.state.get('running'):
                return self.status()
            rows = json.loads(self.path.read_text())
            self.state = {'running':True, 'run_id':uuid.uuid4().hex, 'completed':0, 'total':0, 'failed':[], 'new_slugs':[], 'remaining':None, 'baseline':[c['slug'] for c in rows], 'company_count':len(rows), 'current_batch':'Loading batch list', 'error':''}
            self._checkpoint()
            self._launch()
            return self.status()

    def _run(self):
        try:
            with self.lock:
                needs_batches = self.state.get('remaining') is None
            if needs_batches:
                batch_names = [b['batch'] for b in self.fetch_batches()]
                with self.lock:
                    self.state['remaining'] = batch_names
                    self.state['total'] = len(batch_names)
                    self._checkpoint()
            rows = {c['slug']:c for c in json.loads(self.path.read_text())}
            baseline = set(self.state.get('baseline',rows))
            new = set(self.state.get('new_slugs',[]))
            while True:
                with self.lock:
                    if not self.state['remaining']:
                        break
                    batch = self.state['remaining'][0]
                    self.state['current_batch'] = batch
                    self._checkpoint()
                try:
                    fetched = self.fetch_companies(batch)
                    for company in fetched:
                        previous = rows.get(company['slug'],{})
                        if company['slug'] not in baseline:
                            new.add(company['slug'])
                        merged = {**previous, **company, 'directory_updated_at':time.time()}
                        if not merged.get('linkedin'):
                            merged['linkedin'] = previous.get('linkedin','')
                        rows[company['slug']] = merged
                    self._write(self.path,list(rows.values()))
                except Exception:
                    with self.lock:
                        self.state['failed'].append(batch)
                with self.lock:
                    self.state['remaining'].pop(0)
                    self.state['completed'] += 1
                    self.state['new_slugs'] = sorted(new)
                    self.state['company_count'] = len(rows)
                    self._checkpoint()
            with self.lock:
                self.state['running'] = False
                self.state['current_batch'] = ''
                self.state['finished_at'] = time.time()
                self._checkpoint()
        except Exception as error:
            with self.lock:
                self.state['running'] = False
                self.state['error'] = str(error)
                self._checkpoint()
