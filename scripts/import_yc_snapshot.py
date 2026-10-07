"""Convert the bundled scraper's paired Excel exports to the explorer schema."""
import json
import re
import sys
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}

def read_rows(path):
    with zipfile.ZipFile(path) as archive:
        shared = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            shared = [''.join(t.text or '' for t in item.findall('.//s:t', NS)) for item in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('s:si', NS)]
        rows = []
        for row in ET.fromstring(archive.read('xl/worksheets/sheet1.xml')).findall('s:sheetData/s:row', NS):
            values = {}
            for cell in row.findall('s:c', NS):
                value = cell.find('s:v', NS)
                text = value.text or '' if value is not None else ''.join(t.text or '' for t in cell.findall('.//s:t', NS))
                values[re.sub(r'\d', '', cell.attrib['r'])] = shared[int(text)] if cell.get('t') == 's' and text else text
            rows.append(values)
        headers = rows.pop(0)
        return [{label: row.get(col, '') for col, label in headers.items()} for row in rows]

def convert(folder):
    links = read_rows(folder / 'y_combinator.xlsx')
    records = read_rows(folder / 'yc_finaldata.xlsx')
    if len(links) != len(records):
        raise ValueError('Snapshot profile rows must match the original ordered URL list.')
    out = []
    for link, record in zip(links, records):
        slug = link.get('Links', '').rstrip('/').rsplit('/', 1)[-1]
        if not re.fullmatch(r'[a-z0-9-]{1,100}', slug):
            raise ValueError('Invalid YC profile URL in snapshot.')
        founders = []
        for i in (1, 2):
            if record.get(f'P{i}_name'):
                founders.append({'name': record[f'P{i}_name'], 'title': record.get(f'P{i}_Designation', ''), 'linkedin': record.get(f'P{i}_Linkedin', ''), 'twitter': record.get(f'P{i}_twitter', ''), 'emails_found': [], 'email_guesses': []})
        tags = [t.strip() for t in record.get('Company_tag', '').split(',') if t.strip()]
        out.append({'slug': slug, 'name': record.get('Company_name') or slug, 'website': record.get('website', ''), 'one_liner': record.get('Company_slogan', ''), 'description': record.get('Company_description', ''), 'location': record.get('Location', ''), 'team_size': record.get('Team Size', ''), 'founded': record.get('Founded', ''), 'linkedin': record.get('Company_Linkedin', ''), 'twitter': record.get('Company_X', ''), 'tags': tags, 'status': next((t.title() for t in tags if t in ('ACTIVE', 'INACTIVE', 'ACQUIRED', 'PUBLIC')), ''), 'batch': '', 'industry': '', 'founders': founders, 'site_emails': [], 'profile_source': 'Local scraper snapshot', 'snapshot_stale': True})
    return out

if __name__ == '__main__':
    rows = convert(ROOT / 'ycombinator-com-companies-scraper')
    destination = ROOT / 'data/yc_companies.json'
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(rows, ensure_ascii=False))
    print(f'Imported {len(rows)} local company profiles into {destination.name}.')
