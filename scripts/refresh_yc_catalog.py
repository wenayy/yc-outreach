"""Merge all current public YC batches into the imported company catalog."""
import concurrent.futures
import json
import sys
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api.yc import batches, companies

def refresh():
    path = ROOT / 'data/yc_companies.json'
    catalog = {c['slug']: c for c in json.loads(path.read_text())}
    original = set(catalog)
    batch_list = [b['batch'] for b in batches()]
    failures = []
    with concurrent.futures.ThreadPoolExecutor(3) as pool:
        pending = {pool.submit(companies, batch): batch for batch in batch_list}
        for future in concurrent.futures.as_completed(pending):
            batch = pending[future]
            try:
                for company in future.result():
                    previous = catalog.get(company['slug'], {})
                    if not company.get('linkedin') and previous.get('linkedin'):
                        company['linkedin'] = previous['linkedin']
                    catalog[company['slug']] = {**previous, **company, 'directory_updated_at': int(time.time())}
                print(f'Refreshed {batch}', flush=True)
            except Exception as exc:
                failures.append(batch)
                print(f'Failed {batch}: {exc}', flush=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(list(catalog.values()), ensure_ascii=False))
    temporary.replace(path)
    print(f'Saved {len(catalog)} companies; {len(set(catalog)-original)} new; {len(failures)} batches failed.')
    if failures:
        print('Retry to refresh failed batches:', ', '.join(failures))

if __name__ == '__main__':
    refresh()
