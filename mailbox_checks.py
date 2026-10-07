"""Import mailbox evidence from a fixed Apify actor; read hard-bounce DSNs."""
import datetime as dt
import email
import imaplib
import json
import re
import urllib.request

VERIFIER = 'automation-lab~smtp-email-verifier'
BOUNCE_VERIFIER = 'bounceverify~bounceverify-email-verifier'
PROVIDERS = {'smtp': VERIFIER, 'bounceverify': BOUNCE_VERIFIER}
CHECK_TTL = 7 * 86400
ID = re.compile(r'[A-Za-z0-9]{1,64}\Z')


def apify_get(path, token):
    request = urllib.request.Request('https://api.apify.com/v2/' + path,
                                     headers={'Authorization': 'Bearer ' + token})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            data = response.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError('Verification results are too large.')
        return json.loads(data)
    except Exception:
        # Never surface request headers, credentials or provider response text.
        raise ValueError('Could not import Apify evidence. Check the token and completed run.') from None


def mailbox_verdict(item, provider='smtp'):
    if provider == 'bounceverify':
        status = str(item.get('status', 'unknown')).lower()
        if (status == 'valid' and item.get('smtp_valid') is True
                and item.get('is_catch_all') is False and item.get('is_disposable') is False
                and item.get('syntax_valid') is True and item.get('domain_exists') is True
                and item.get('mx_found') is True
                and str(item.get('risk_type') or '').strip().lower() in ('', 'none')
                and not item.get('error')):
            return 'deliverable'
        if item.get('is_disposable') is True or status in ('risky', 'disposable'):
            if item.get('is_disposable') is not True and item.get('is_catch_all') is True:
                return 'catch-all'
            return 'risky'
        if item.get('is_catch_all') is True or status in ('catch-all', 'catch_all'):
            return 'catch-all'
        if status == 'invalid':
            return 'invalid'
        return 'unknown'
    if provider != 'smtp':
        return 'unknown'
    status = str(item.get('smtpStatus', 'unknown')).lower()
    if (status == 'valid' and item.get('isValid') is True
            and item.get('isCatchAll') is False and item.get('isDisposable') is False
            and item.get('isValidSyntax') is True and item.get('hasMxRecords') is True
            and item.get('smtpCode') == 250 and not item.get('error')):
        return 'deliverable'
    if status == 'invalid':
        return 'invalid'
    if status == 'risky' or item.get('isDisposable') is True:
        return 'risky'
    if status == 'catch-all' or item.get('isCatchAll') is True:
        return 'catch-all'
    return 'unknown'


def import_run(run_id, token, now, fetch=apify_get, provider='smtp'):
    if provider not in PROVIDERS:
        raise ValueError('Unsupported mailbox verifier.')
    if not isinstance(run_id, str) or not ID.fullmatch(run_id):
        raise ValueError('Invalid Apify run ID.')
    if not isinstance(token, str) or not token or len(token) > 512 or '\n' in token or '\r' in token:
        raise ValueError('Enter an Apify token.')
    actor = fetch('acts/' + PROVIDERS[provider], token).get('data', {})
    run = fetch('actor-runs/' + run_id, token).get('data', {})
    if not isinstance(actor, dict) or not isinstance(run, dict):
        raise ValueError('Unexpected Apify run metadata.')
    if run.get('actId') != actor.get('id') or not actor.get('id') or run.get('status') != 'SUCCEEDED':
        raise ValueError('Only completed mailbox-verifier runs can authorize sending.')
    try:
        checked = dt.datetime.fromisoformat(run['finishedAt'].replace('Z', '+00:00')).timestamp()
    except (KeyError, TypeError, ValueError):
        raise ValueError('The verification run has no usable completion timestamp.') from None
    if not now - CHECK_TTL <= checked <= now + 60:
        raise ValueError('This verification is older than seven days. Run a new mailbox check.')
    dataset = run.get('defaultDatasetId', '')
    if not ID.fullmatch(dataset):
        raise ValueError('No verification dataset available.')
    input_store = run.get('defaultKeyValueStoreId', '')
    if not ID.fullmatch(input_store):
        raise ValueError('No verifier input evidence available.')
    run_input = fetch('key-value-stores/' + input_store + '/records/INPUT', token)
    if not isinstance(run_input, dict) or (provider == 'smtp' and run_input.get('catchAllTest', True) is not True):
        raise ValueError('Catch-all detection must be enabled for a sendable check.')
    input_emails = run_input.get('emails', [])
    if provider == 'bounceverify' and (not isinstance(input_emails, list) or not input_emails
            or any(not isinstance(address, str) for address in input_emails)):
        raise ValueError('No verifier address input evidence available.')
    requested = {address.strip().lower() for address in input_emails if isinstance(address, str)}
    items = fetch('datasets/' + dataset + '/items?clean=true&format=json&limit=1000', token)
    if not isinstance(items, list):
        raise ValueError('Unexpected verification output.')
    return [(str(item.get('email', '')).strip().lower(), mailbox_verdict(item, provider), checked, run_id)
            for item in items if isinstance(item, dict)
            and (provider != 'bounceverify' or str(item.get('email', '')).strip().lower() in requested)]


def hard_bounce_recipients(raw):
    """Use structured DSNs only, never infer a bounce from quoted message text."""
    result = set()
    message = email.message_from_bytes(raw)
    if message.get_content_type() != 'multipart/report' or message.get_param('report-type') != 'delivery-status':
        return result
    for part in message.walk():
        if part.get_content_type() != 'message/delivery-status':
            continue
        blocks = part.get_payload()
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if str(block.get('Action', '')).lower() != 'failed' or not str(block.get('Status', '')).startswith('5.'):
                continue
            address = str(block.get('Final-Recipient', '')).partition(';')[2].strip().lower()
            if address:
                result.add(address)
    return result


def gmail_bounces(account, now):
    if not account['user'].lower().endswith('@gmail.com'):
        raise ValueError('Bounce scanning currently supports Gmail accounts only.')
    since = dt.datetime.fromtimestamp(now - 14 * 86400, dt.timezone.utc).strftime('%d-%b-%Y')
    with imaplib.IMAP4_SSL('imap.gmail.com', timeout=20) as inbox:
        inbox.login(account['user'], account['password'])
        inbox.select('INBOX', readonly=True)
        status, data = inbox.uid('search', None, 'SINCE', since, 'OR', 'FROM', '"mailer-daemon"', 'FROM', '"postmaster"')
        if status != 'OK':
            raise ValueError('Could not search delivery reports.')
        addresses = set()
        for uid in data[0].split()[-100:]:
            status, parts = inbox.uid('fetch', uid, '(BODY.PEEK[])')
            if status == 'OK':
                for part in parts:
                    if isinstance(part, tuple) and isinstance(part[1], bytes) and len(part[1]) <= 2_000_000:
                        addresses.update(hard_bounce_recipients(part[1]))
        return addresses
