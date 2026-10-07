import json
import tempfile
import threading
import unittest
from pathlib import Path
from catalog_jobs import CatalogJobs

class CatalogJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.seed = self.folder / 'seed.json'
        self.seed.write_text(json.dumps([{'slug':'old','name':'Old','linkedin':'https://linkedin.com/company/old','founders':[{'name':'Founder'}]}]))

    def tearDown(self):
        self.temp.cleanup()

    def test_scan_is_background_and_repeated_start_attaches_to_same_job(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(batch):
            entered.set()
            release.wait(5)
            return [{'slug':'new','name':'New'}]
        jobs = CatalogJobs(self.folder,self.seed,lambda:[{'batch':'Winter 2024'}],fetch)
        started = jobs.start()
        self.assertTrue(entered.wait(2))
        self.assertTrue(jobs.status()['running'])
        self.assertEqual(jobs.start()['run_id'],started['run_id'])
        release.set()
        jobs.thread.join(3)
        self.assertFalse(jobs.status()['running'])
        self.assertEqual(jobs.status()['new_slugs'],['new'])
        self.assertEqual(len(json.loads(jobs.path.read_text())),2)

    def test_interrupted_scan_resumes_saved_remaining_batches(self):
        (self.folder/'company_catalog.json').write_text(json.dumps([{'slug':'old','name':'Old'},{'slug':'first','name':'First'}]))
        (self.folder/'catalog_job.json').write_text(json.dumps({'running':True,'run_id':'saved','remaining':['Summer 2024'],'baseline':['old'],'new_slugs':['first'],'failed':[],'completed':1,'total':2}))
        fetched=[]
        jobs=CatalogJobs(self.folder,self.seed,lambda:self.fail('Must not refetch completed batch list'),lambda b:fetched.append(b) or [{'slug':'second','name':'Second'}])
        jobs.thread.join(3)
        self.assertEqual(fetched,['Summer 2024'])
        self.assertEqual(jobs.status()['completed'],2)
        self.assertEqual(set(jobs.status()['new_slugs']),{'first','second'})
        self.assertEqual(len(json.loads(jobs.path.read_text())),3)

    def test_failed_batch_does_not_discard_profiles_or_other_new_records(self):
        def fetch(batch):
            if batch=='bad':raise RuntimeError('Provider failed')
            return [{'slug':'old','name':'Updated','linkedin':''},{'slug':'new','name':'New'}]
        jobs=CatalogJobs(self.folder,self.seed,lambda:[{'batch':'bad'},{'batch':'good'}],fetch)
        jobs.start();jobs.thread.join(3)
        rows={c['slug']:c for c in json.loads(jobs.path.read_text())}
        self.assertEqual(jobs.status()['failed'],['bad'])
        self.assertEqual(rows['old']['founders'],[{'name':'Founder'}])
        self.assertEqual(rows['old']['linkedin'],'https://linkedin.com/company/old')
        self.assertIn('new',rows)

if __name__=='__main__':unittest.main()
