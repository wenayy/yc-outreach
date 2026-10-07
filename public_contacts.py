"""Bounded discovery of published company emails, with page provenance."""
import concurrent.futures as cf
import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from contact_emails import extract_emails, founder_emails


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.anchors = []
        self.structured = []
        self.anchor = None
        self.script = None

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.extend(value for key, value in attrs if key == 'href' and value)
            self.anchor = [dict(attrs).get('href', ''), '']
        if tag == 'script' and dict(attrs).get('type') == 'application/ld+json':
            self.script = ''

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor[1] += data
        if self.script is not None:
            self.script += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor is not None:
            self.anchors.append(self.anchor)
            self.anchor = None
        if tag == 'script' and self.script is not None:
            try:
                self.structured.append(json.loads(self.script))
            except (ValueError, TypeError):
                pass
            self.script = None


def public_url(url, domain):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower()
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise ValueError('Unsupported website URL.')
    if host.removeprefix('www.') != domain:
        raise ValueError('Only this company website is searched.')
    addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == 'https' else 80))
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('Website must use a public network address.')
    return url


class Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, domain):
        self.domain = domain

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl, self.domain)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_page(url, domain):
    try:
        public_url(url, domain)
        opener = urllib.request.build_opener(Redirects(domain), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        with opener.open(urllib.request.Request(url, headers={'User-Agent': 'YCOutreach public contact lookup'}), timeout=4) as response:
            content_type = response.headers.get('Content-Type', '').lower()
            if 'html' not in content_type and not (domain == 'api.github.com' and 'json' in content_type):
                return url, '', 'Page is not HTML.'
            return response.geturl(), response.read(750_000).decode('utf-8', 'ignore'), ''
    except Exception:
        return url, '', 'Public page unavailable, login required, or redirect outside the source host.'


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def platform(url):
    host = (urllib.parse.urlsplit(url).hostname or '').lower().removeprefix('www.')
    return 'GitHub' if host in ('github.com', 'api.github.com') else 'LinkedIn' if host == 'linkedin.com' else 'X' if host in ('x.com', 'twitter.com') else 'Public profile'


def person_profile(url):
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower().removeprefix('www.')
    path = parsed.path.strip('/')
    if parsed.scheme not in ('https', 'http'):
        return False
    if host == 'github.com':
        return bool(re.fullmatch(r'[A-Za-z0-9-]{1,39}', path))
    if host == 'linkedin.com':
        return path.startswith('in/') and len(path.split('/')) == 2
    if host in ('x.com', 'twitter.com'):
        return bool(re.fullmatch(r'[A-Za-z0-9_]{1,15}', path)) and path not in ('intent', 'share', 'home')
    return False


def discover(website, domain, people, reader=read_page):
    people = [dict(person) for person in people]
    base = website if '://' in website else 'https://' + website
    base = base.rstrip('/') + '/'
    if not domain:
        return {'contacts': [], 'pages': [], 'error': 'Company has no website domain.'}
    first = reader(base, domain)
    parser = Links()
    parser.feed(first[1])
    urls = [base + path for path in ('team', 'about', 'contact', 'about-us', 'leadership')]
    for href in parser.links:
        candidate = urllib.parse.urljoin(base, href).split('#')[0]
        parsed = urllib.parse.urlsplit(candidate)
        if parsed.hostname and parsed.hostname.lower().removeprefix('www.') == domain and any(word in parsed.path.lower() for word in ('team', 'about', 'contact', 'people', 'founder', 'leadership')):
            urls.insert(0, candidate)
    urls = list(dict.fromkeys(urls))[:7]
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        pages = [first] + list(pool.map(lambda u: reader(u, domain), urls))
    sources = {}
    profile_tasks = {}
    direct = []
    for person in people:
        for key in ('linkedin', 'twitter', 'github', 'website'):
            link = person.get(key)
            if isinstance(link, str) and link.startswith(('https://', 'http://')):
                profile_tasks.setdefault(link, person)
    for url, body, error in pages:
        parsed_page = Links()
        parsed_page.feed(body)
        for node in nodes(parsed_page.structured):
            types = node.get('@type', [])
            if isinstance(types, str):
                types = [types]
            name = node.get('name', '')
            title = node.get('jobTitle', '')
            # Only company-published, relevant named team members are added.
            if 'Person' not in types or not isinstance(name, str) or len(name.split()) < 2 or not isinstance(title, str):
                continue
            if not re.search(r'founder|chief|ceo|cto|engineer|recruit|talent|hiring|director|head of', title, re.I):
                continue
            employer = node.get('worksFor')
            employer_url = employer.get('url', '') if isinstance(employer, dict) else ''
            if isinstance(employer_url, str) and employer_url and (urllib.parse.urlsplit(employer_url).hostname or '').lower().removeprefix('www.') != domain:
                continue
            person = next((p for p in people if p.get('name', '').casefold() == name.casefold()), None)
            if person is None:
                person = {'name': name, 'title': title}
                people.append(person)
            for email in extract_emails(str(node.get('email', '')), domain):
                if email.split('@')[0] not in ('info', 'hello', 'contact', 'support', 'sales', 'team', 'jobs', 'careers', 'noreply', 'no-reply'):
                    direct.append((person, email, url, 'Company-published Person record'))
            links = node.get('sameAs', [])
            if isinstance(links, str):
                links = [links]
            for link in links if isinstance(links, list) else []:
                if isinstance(link, str) and person_profile(link):
                    profile_tasks.setdefault(link, person)
        for href, label in parsed_page.anchors:
            link = urllib.parse.urljoin(url, href).split('#')[0]
            person = next((p for p in people if ' '.join(label.split()).casefold() == p.get('name', '').casefold()), None)
            label = ' '.join(label.split())
            if person is None and person_profile(link) and re.search(r'team|people|leadership', urllib.parse.urlsplit(url).path, re.I) and re.fullmatch(r'[A-Z][a-z]+(?: [A-Z][a-z]+){1,3}', label) and not re.search(r'LinkedIn|GitHub|Follow|View|Profile', label, re.I):
                person = {'name': label, 'title': 'Team member', '_company_source': url}
                people.append(person)
            if person is not None and urllib.parse.urlsplit(link).hostname:
                profile_tasks.setdefault(link, person)
        for email in extract_emails(body, domain):
            sources.setdefault(email, []).append(url)
    # Up to eight profiles already linked to a known person; no login/cookie access.
    def read_profile(task):
        link, person = task
        host = (urllib.parse.urlsplit(link).hostname or '').lower().removeprefix('www.')
        fetch_url = 'https://api.github.com/users/' + urllib.parse.urlsplit(link).path.strip('/') if host == 'github.com' and person_profile(link) else link
        fetch_host = urllib.parse.urlsplit(fetch_url).hostname.removeprefix('www.')
        _, body, error = reader(fetch_url, fetch_host)
        if fetch_host == 'api.github.com':
            try:
                user = json.loads(body)
                if user.get('type') != 'User' or str(user.get('name') or '').strip().casefold() != person['name'].strip().casefold():
                    return link, person, '', 'Profile name does not match the linked person.'
                body = str(user.get('email') or '')
            except (ValueError, AttributeError):
                body = ''
        return link, person, body, error
    tasks = list(profile_tasks.items())[:8]
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        for link, person, body, error in pool.map(read_profile, tasks):
            pages.append((link, body, error))
            for email in founder_emails(person['name'], extract_emails(body, domain), [p['name'] for p in people]):
                direct.append((person, email, link, 'Published work email on a linked public profile'))
    names = [person.get('name', '') for person in people]
    contacts = []
    for person in people:
        for email in founder_emails(person.get('name', ''), list(sources), names):
            contacts.append({'name': person['name'], 'title': person.get('title', ''), 'email': email,
                             'source_urls': sorted(set(sources[email])), 'attribution': 'Published company email matches a unique known person’s name pattern',
                             'source_platform': 'Company website', 'found_at': int(time.time())})
    for person, email, url, attribution in direct:
        existing = next((c for c in contacts if c['email'] == email), None)
        if existing and existing['name'].casefold() != person['name'].casefold():
            continue
        if existing:
            existing['source_urls'] = sorted(set(existing['source_urls'] + [url]))
        else:
            contacts.append({'name': person['name'], 'title': person.get('title', ''), 'email': email,
                             'source_urls': sorted(set([url] + ([person['_company_source']] if person.get('_company_source') else []))), 'source_platform': platform(url) if url in profile_tasks else 'Company website',
                             'attribution': attribution, 'found_at': int(time.time())})
    return {'contacts': contacts, 'pages': [{'url': url, 'read': bool(body), 'error': error} for url, body, error in pages],
            'general_emails': [{'email': email, 'source_urls': sorted(set(urls))} for email, urls in sources.items()
                               if email not in {contact['email'] for contact in contacts}]}
