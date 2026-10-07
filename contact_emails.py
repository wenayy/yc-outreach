"""Conservative public-email extraction and founder attribution."""
import html
import re
import unicodedata
import urllib.parse

EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')
BAD_EMAIL = re.compile(r'\.(png|jpe?g|gif|svg|webp|css|js)$|sentry|wixpress|example\.|@2x|u00|domain\.com|email\.com', re.I)


def extract_emails(body, domain):
    body = urllib.parse.unquote(html.unescape(body or ''))
    # Common explicit obfuscation, without converting arbitrary prose into addresses.
    body = re.sub(r'\s*[\[(]\s*at\s*[\])]\s*', '@', body, flags=re.I)
    body = re.sub(r'\s*[\[(]\s*dot\s*[\])]\s*', '.', body, flags=re.I)
    return sorted({e.lower().strip('.') for e in EMAIL_RE.findall(body)
                   if not BAD_EMAIL.search(e) and domain and
                   (e.split('@')[1].lower() == domain or e.split('@')[1].lower().endswith('.' + domain))})


def founder_match(name, email):
    parts = re.sub(r'[^a-z ]', '', unicodedata.normalize('NFKD', name)
                   .encode('ascii', 'ignore').decode().lower()).split()
    if not parts:
        return False
    first, last = parts[0], parts[-1]
    patterns = {first}
    if len(parts) > 1:
        patterns.update((first + '.' + last, first + '_' + last, first + '-' + last,
                         first + last, first[0] + last, first[0] + '.' + last))
    return email.split('@')[0].lower() in patterns


def founder_emails(name, emails, founder_names):
    """Exclude addresses whose name pattern could refer to several founders."""
    return [email for email in emails if founder_match(name, email)
            and sum(founder_match(other, email) for other in founder_names) == 1]
