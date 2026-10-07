import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from api import yc


class OutreachCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'catalog.json'
        self.path.write_text(json.dumps([
            {'slug': 'saved', 'name': 'Saved company', 'batch': 'Winter 2024'},
            {'slug': 'duplicate', 'name': 'Old name', 'batch': 'Winter 2024', 'linkedin': 'https://linkedin.com/company/example'},
            {'slug': 'snapshot', 'name': 'Scraper company', 'batch': '', 'snapshot_stale': True},
        ]))
        self.catalog = patch.object(yc, 'CATALOG_PATH', self.path)
        self.catalog.start()

    def tearDown(self):
        self.catalog.stop()
        self.temp.cleanup()

    def test_outreach_combines_live_and_saved_companies_without_duplicates(self):
        with patch.object(yc, 'companies', return_value=[{'slug': 'duplicate', 'name': 'Current name', 'batch': 'Winter 2024'}, {'slug': 'new', 'name': 'New company', 'batch': 'Winter 2024'}]):
            status, rows, cache = yc.route('action=companies&batch=Winter%202024')
        self.assertEqual(status, 200)
        self.assertEqual(cache, 0)
        records = {c['slug']: c for c in rows}
        self.assertEqual(set(records), {'saved', 'duplicate', 'new'})
        self.assertEqual(records['duplicate']['name'], 'Current name')
        self.assertIn('linkedin', records['duplicate'])

    def test_snapshot_without_batch_is_accessible_and_offline_catalog_still_loads(self):
        with patch.object(yc, 'batches', side_effect=RuntimeError('offline')):
            _, options, _ = yc.route('action=batches')
        self.assertIn({'batch': 'Unspecified', 'count': 1}, options)
        with patch.object(yc, 'companies', side_effect=RuntimeError('offline')):
            _, rows, _ = yc.route('action=companies&batch=Unspecified')
        self.assertEqual([c['slug'] for c in rows], ['snapshot'])


if __name__ == '__main__':
    unittest.main()
