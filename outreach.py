"""Durable, local-only SMTP queue. No third-party dependencies."""
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
import time
from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from mailbox_checks import CHECK_TTL, import_run, gmail_bounces
from send_schedule import DEFAULT as DEFAULT_SCHEDULE, valid_zone, validate as validate_schedule, next_window

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\Z")
INTERVAL = 120
DAILY_MAX = 1000
SENDER_DAILY_MAX = 500


def load_env(path):
    """Small .env loader; existing environment variables take precedence."""
    if not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if re.fullmatch(r'[A-Z][A-Z0-9_]*', key.strip()):
            os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


class MailQueue:
    def __init__(self, path, transport=None, now=time.time):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.now = now
        self.transport = transport or self.send_smtp
        self.stop_event = threading.Event()
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS messages (
                  id INTEGER PRIMARY KEY, company TEXT, recipient TEXT UNIQUE,
                  subject TEXT, body TEXT, source TEXT, status TEXT DEFAULT 'pending',
                  created REAL, attempted REAL, sent REAL, error TEXT DEFAULT '',
                  priority INTEGER NOT NULL DEFAULT 9);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS suppressed (email TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS email_checks (
                  email TEXT PRIMARY KEY, verdict TEXT NOT NULL, checked REAL NOT NULL,
                  run_id TEXT NOT NULL);
                INSERT OR IGNORE INTO settings VALUES ('paused', '1');
                INSERT OR IGNORE INTO settings VALUES ('daily_limit', '1000');
                INSERT OR IGNORE INTO settings VALUES ('next_send', '0');
                INSERT OR IGNORE INTO settings VALUES ('interval_minutes', '2');
                INSERT OR IGNORE INTO settings VALUES ('senders', '["primary"]');
                INSERT OR IGNORE INTO settings VALUES ('allow_catch_all', '0');
                INSERT OR IGNORE INTO settings VALUES ('allow_unverified', '0');
            ''')
            db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', ('schedule', json.dumps(DEFAULT_SCHEDULE)))
            columns = {r[1] for r in db.execute('PRAGMA table_info(messages)')}
            if 'sender' not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN sender TEXT NOT NULL DEFAULT ''")
            if 'unverified_ack' not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN unverified_ack INTEGER NOT NULL DEFAULT 0")
            if 'planned_sender' not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN planned_sender TEXT NOT NULL DEFAULT ''")
            if 'priority' not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN priority INTEGER NOT NULL DEFAULT 9")
            if 'variants' not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN variants TEXT NOT NULL DEFAULT '[]'")
            if 'template' not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN template TEXT NOT NULL DEFAULT ''")
            if 'recipient_timezone' not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN recipient_timezone TEXT NOT NULL DEFAULT ''")
                db.execute("ALTER TABLE messages ADD COLUMN timezone_source TEXT NOT NULL DEFAULT 'fallback'")
            # Also enforce the recipient identity at the database boundary,
            # including direct writes that bypass enqueue's normalization.
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS messages_recipient_normalized ON messages(lower(trim(recipient)))")
            # Earlier queue rows did not store a delivery priority.  Give them
            # the same ordering a newly reviewed message receives.
            db.execute("""UPDATE messages SET priority=CASE source
                WHEN 'valid' THEN 0 WHEN 'site' THEN 1
                WHEN 'manual' THEN 2 WHEN 'unknown' THEN 2
                WHEN 'guess' THEN 3 ELSE 9 END WHERE priority=9""")
            # Attribute historical sends to the existing primary mailbox.
            db.execute("UPDATE messages SET sender=? WHERE sender='' AND attempted IS NOT NULL",
                       (self.config()['sender'].lower(),))
            # A crash during SMTP has an unknown outcome. Never resend automatically.
            db.execute("UPDATE messages SET status='uncertain', error='Server stopped during sending. Check your mailbox before contacting again.' WHERE status='sending'")
            db.execute("UPDATE settings SET value='1' WHERE key='paused'")
            self.hold_unverified(db)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def normalize_daily_limit(self, db):
        legacy_values = {25, 200}
        current = int(self.settings(db).get('daily_limit', str(DAILY_MAX)))
        if current in legacy_values:
            db.execute("UPDATE settings SET value=? WHERE key='daily_limit'", (str(DAILY_MAX),))
            current = DAILY_MAX
        return current

    @staticmethod
    def settings(db):
        return dict(db.execute('SELECT key, value FROM settings'))

    @staticmethod
    def config(sender_id='primary'):
        prefix = {'primary': 'SMTP_', 'second': 'SMTP_2_', 'third': 'SMTP_3_'}.get(sender_id)
        if not prefix:
            raise ValueError('Unknown sender account.')
        def env(key, default=''):
            return os.environ.get(prefix + key, default)
        port = env('PORT', '587')
        return {
            'id': sender_id, 'host': env('HOST', 'smtp.gmail.com' if sender_id != 'primary' else ''),
            'port': int(port) if port.isdigit() else 0, 'user': env('USER'),
            'password': env('PASSWORD'), 'sender': env('FROM', env('USER')).strip().lower(),
            'name': env('FROM_NAME', os.environ.get('SMTP_FROM_NAME', '')),
            'security': env('SECURITY', 'starttls'),
        }

    @staticmethod
    def valid_config(c):
        return bool(c['host'] and c['user'] and c['password'] and EMAIL.fullmatch(c['sender'])
                    and c['security'] in ('starttls', 'ssl') and 0 < c['port'] < 65536)

    def configured(self):
        return any(self.valid_config(self.config(key)) for key in ('primary', 'second', 'third'))

    def status(self):
        with self.lock, self.connect() as db:
            s = self.settings(db)
            limit = self.normalize_daily_limit(db)
            db.row_factory = sqlite3.Row
            self.hold_unverified(db)
            messages = [dict(r) for r in db.execute('SELECT id, company, recipient, planned_sender, sender, source, status, sent, error, priority, template, recipient_timezone, timezone_source FROM messages ORDER BY id DESC')]
            schedule = json.loads(s['schedule'])
            now = self.now()
            campaign_due = self.campaign_due(db, now)
            for message in messages:
                message['effective_timezone'] = message['recipient_timezone'] or schedule['fallback_timezone']
                message['scheduled_for'] = self.recipient_due(db, message, max(now, campaign_due), schedule) if schedule['enabled'] and message['status'] == 'pending' else None
            if schedule['enabled']:
                self.refresh_next_send(db)
                s = self.settings(db)
            checks = {r['email']: dict(r) for r in db.execute('SELECT * FROM email_checks')}
            accounts = []
            for key in ('primary', 'second', 'third'):
                c = self.config(key)
                if c['sender'] or key == 'primary':
                    used = db.execute('SELECT COUNT(*) FROM messages WHERE sender=? AND attempted>?',
                                      (c['sender'], self.now() - 86400)).fetchone()[0]
                    accounts.append({'id': key, 'email': c['sender'], 'configured': self.valid_config(c),
                                     'used': used, 'daily_limit': SENDER_DAILY_MAX})
            return {'available': True, 'configured': self.configured(), 'sender': self.config()['sender'],
                    'accounts': accounts, 'selected_senders': json.loads(s['senders']),
                    'paused': s['paused'] == '1', 'daily_limit': limit,
                    'interval_minutes': int(s['interval_minutes']), 'interval_scope': 'per_account',
                    'next_send': float(s['next_send']),
                    'messages': messages, 'checks': checks, 'verification_required': True,
                    'allow_catch_all': s.get('allow_catch_all', '0') == '1',
                    'allow_unverified': s.get('allow_unverified', '0') == '1',
                    'schedule': schedule,
                    'suppressed': [r[0] for r in db.execute('SELECT email FROM suppressed')],
                    'token': self.token}

    def enqueue(self, messages):
        if not isinstance(messages, list) or not 1 <= len(messages) <= 1000:
            raise ValueError('Choose between 1 and 1000 reviewed emails per queue request.')
        cleaned = []
        for m in messages:
            if not isinstance(m, dict):
                raise ValueError('Invalid message.')
            recipient = str(m.get('recipient', '')).strip().lower()
            subject, body = str(m.get('subject', '')).strip(), str(m.get('body', '')).strip()
            source = m.get('source')
            if not EMAIL.fullmatch(recipient) or len(recipient) > 254:
                raise ValueError('A recipient email address is invalid.')
            if source not in ('verified', 'valid', 'site', 'manual', 'guess', 'unknown'):
                raise ValueError('Invalid recipient source.')
            unverified_ack = source in ('guess', 'unknown') and m.get('allow_unverified') is True
            if source in ('guess', 'unknown') and not unverified_ack:
                raise ValueError('Select the unverified address explicitly before adding it to the queue.')
            if not subject or '\r' in subject or '\n' in subject or len(subject) > 200:
                raise ValueError('Use a non-empty, single-line subject up to 200 characters.')
            if not body or len(body) > 20000:
                raise ValueError('Use a non-empty body up to 20,000 characters.')
            if re.search(r'\{\w+\}|\[1-2 lines|\[1 line', subject + '\n' + body):
                raise ValueError('Finish the template placeholders before queueing.')
            variants = m.get('variants', [])
            if not isinstance(variants, list) or (variants and len(variants) != 2):
                raise ValueError('Rotation requires exactly two reviewed drafts.')
            reviewed = []
            for variant in variants:
                if not isinstance(variant, dict):
                    raise ValueError('Invalid template draft.')
                vs, vb = str(variant.get('subject', '')).strip(), str(variant.get('body', '')).strip()
                if not vs or '\r' in vs or '\n' in vs or len(vs) > 200 or not vb or len(vb) > 20000:
                    raise ValueError('Both templates need a valid subject and body.')
                if re.search(r'\{\w+\}|\[1-2 lines|\[1 line', vs + '\n' + vb):
                    raise ValueError('Finish both template drafts before queueing.')
                reviewed.append({'subject': vs, 'body': vb})
            priority = {'verified': 0, 'valid': 0, 'site': 1, 'manual': 2, 'unknown': 2, 'guess': 3}[source]
            zone = valid_zone(m.get('recipient_timezone', ''), optional=True)
            zone_source = m.get('timezone_source', 'manual' if zone else 'fallback')
            if zone_source not in ('manual', 'company location', 'fallback'):
                raise ValueError('Invalid time zone source.')
            cleaned.append((str(m.get('company', ''))[:200], recipient, subject, body, source, self.now(), int(unverified_ack), priority, json.dumps(reviewed), zone, zone_source))
        added = 0
        with self.lock, self.connect() as db:
            for values in cleaned:
                if db.execute('SELECT 1 FROM suppressed WHERE email=?', (values[1],)).fetchone():
                    continue
                if values[0].strip() and db.execute("""SELECT COUNT(*) FROM messages
                    WHERE lower(trim(company))=lower(trim(?))
                    AND status IN ('pending','held','sending','sent','uncertain')""", (values[0],)).fetchone()[0] >= 3:
                    continue
                # A cancelled draft may be replaced only if no delivery was attempted.
                db.execute("DELETE FROM messages WHERE lower(trim(recipient))=? AND status='cancelled' AND attempted IS NULL", (values[1],))
                added += db.execute('INSERT OR IGNORE INTO messages (company,recipient,subject,body,source,created,unverified_ack,priority,variants,recipient_timezone,timezone_source) VALUES (?,?,?,?,?,?,?,?,?,?,?)', values).rowcount
            self.hold_unverified(db)
            self.assign_pending(db, [self.config(key) for key in json.loads(self.settings(db)['senders'])])
        return {'added': added, 'skipped': len(cleaned) - added}

    def hold_unverified(self, db):
        catch_all = 'catch-all' if self.settings(db).get('allow_catch_all') == '1' else 'deliverable'
        db.execute("""UPDATE messages SET status='invalid', error='Mailbox verifier rejected this address.'
            WHERE status IN ('pending','held') AND EXISTS
            (SELECT 1 FROM email_checks c WHERE c.email=messages.recipient AND c.verdict='invalid')""")
        db.execute("""UPDATE messages SET status='held', error='Mailbox verification required before sending.'
            WHERE status='pending' AND NOT EXISTS
            (SELECT 1 FROM email_checks c WHERE c.email=messages.recipient
             AND c.verdict IN ('deliverable',?) AND c.checked>=?)""", (catch_all, self.now() - CHECK_TTL))
        db.execute("""UPDATE messages SET status='pending', error=''
            WHERE status='held' AND NOT EXISTS (SELECT 1 FROM suppressed s WHERE s.email=messages.recipient)
            AND EXISTS (SELECT 1 FROM email_checks c WHERE c.email=messages.recipient
            AND c.verdict IN ('deliverable',?) AND c.checked>=?)""", (catch_all, self.now() - CHECK_TTL))
        if self.settings(db).get('allow_unverified') == '1':
            db.execute("""UPDATE messages SET status='pending', error=''
                WHERE status='held' AND NOT EXISTS (SELECT 1 FROM suppressed s WHERE s.email=messages.recipient)
                AND NOT EXISTS (SELECT 1 FROM email_checks c WHERE c.email=messages.recipient
                    AND c.verdict IN ('invalid','risky','disposable'))""")
        db.execute("""UPDATE messages SET priority=CASE
            WHEN NOT EXISTS (SELECT 1 FROM email_checks c WHERE c.email=messages.recipient AND c.verdict IN ('deliverable','catch-all') AND c.checked>=?) THEN 5
            WHEN EXISTS (SELECT 1 FROM email_checks c WHERE c.email=messages.recipient AND c.verdict='catch-all') THEN 4
            WHEN source IN ('verified','valid') THEN 0 WHEN source='site' THEN 1
            WHEN source IN ('manual','unknown') THEN 2 WHEN source='guess' THEN 3 ELSE 9 END
            WHERE status='pending'""", (self.now() - CHECK_TTL,))

    def apply_checks(self, records):
        with self.lock, self.connect() as db:
            for address, verdict, checked, run_id in records:
                if not EMAIL.fullmatch(address):
                    continue
                db.execute('''INSERT INTO email_checks VALUES (?,?,?,?) ON CONFLICT(email) DO UPDATE SET
                    verdict=excluded.verdict, checked=excluded.checked, run_id=excluded.run_id
                    WHERE excluded.checked>=email_checks.checked''', (address, verdict, checked, run_id))
                current = db.execute('SELECT verdict, checked FROM email_checks WHERE email=?', (address,)).fetchone()
                if current[0] == 'deliverable' and current[1] >= self.now() - CHECK_TTL:
                    db.execute("""UPDATE messages SET status='pending', error=''
                        WHERE recipient=? AND status='held'
                        AND NOT EXISTS (SELECT 1 FROM suppressed WHERE email=?)""", (address, address))
                elif current[0] == 'invalid':
                    db.execute("UPDATE messages SET status='invalid', error='Mailbox verifier rejected this address.' WHERE recipient=? AND status IN ('pending','held')", (address,))
            self.hold_unverified(db)
            self.assign_pending(db, [self.config(key) for key in json.loads(self.settings(db)['senders'])])
        return {'imported': len(records)}

    def mark_bounced(self, address, sender=None):
        if not EMAIL.fullmatch(address):
            raise ValueError('Enter a valid bounced email address.')
        with self.lock, self.connect() as db:
            if sender and not db.execute("SELECT 1 FROM messages WHERE recipient=? AND sender=? AND status='sent'", (address, sender)).fetchone():
                return 0
            db.execute('INSERT OR IGNORE INTO suppressed VALUES (?)', (address,))
            changed = db.execute("UPDATE messages SET status='bounced', error='Hard bounce: recipient could not receive the message.' WHERE recipient=? AND status IN ('sent','pending','held')", (address,)).rowcount
            return changed

    def action(self, payload):
        action = payload.get('action')
        if action == 'set_schedule':
            schedule = validate_schedule(payload.get('schedule'))
            suggestions = payload.get('suggested_timezones', [])
            if not isinstance(suggestions, list) or len(suggestions) > 1000:
                raise ValueError('Pass up to 1000 recipient time zone suggestions.')
            zones = []
            for item in suggestions:
                if not isinstance(item, dict) or not EMAIL.fullmatch(str(item.get('recipient', '')).strip().lower()):
                    raise ValueError('Invalid recipient time zone suggestion.')
                zones.append((valid_zone(item.get('recipient_timezone')), str(item['recipient']).strip().lower()))
            with self.lock, self.connect() as db:
                db.execute("UPDATE settings SET value=? WHERE key='schedule'", (json.dumps(schedule),))
                for zone, address in zones:
                    db.execute("UPDATE messages SET recipient_timezone=?,timezone_source='company location' WHERE recipient=? AND recipient_timezone='' AND status IN ('pending','held')", (zone, address))
                db.execute("UPDATE settings SET value='0' WHERE key='next_send'")
                self.refresh_next_send(db)
            return {'schedule': schedule}
        if action == 'recipient_timezone':
            zone = valid_zone(payload.get('recipient_timezone', ''), optional=True)
            address = str(payload.get('recipient', '')).strip().lower()
            with self.lock, self.connect() as db:
                changed = db.execute("UPDATE messages SET recipient_timezone=?,timezone_source=? WHERE recipient=? AND status IN ('pending','held')", (zone, 'manual' if zone else 'fallback', address)).rowcount
                db.execute("UPDATE settings SET value='0' WHERE key='next_send'")
                self.refresh_next_send(db)
            return {'updated': changed}
        if action == 'enqueue':
            return self.enqueue(payload.get('messages'))
        if action == 'import_checks':
            return self.apply_checks(import_run(payload.get('run_id'), payload.get('apify_token'), self.now(), provider=payload.get('provider', 'smtp')))
        if action == 'set_daily_limit':
            limit = payload.get('daily_limit')
            if type(limit) is not int or not 1 <= limit <= DAILY_MAX:
                raise ValueError('Daily total must be between 1 and 1000.')
            with self.lock, self.connect() as db:
                db.execute("UPDATE settings SET value=? WHERE key='daily_limit'", (str(limit),))
            return {'daily_limit': limit}
        if action in ('allow_catch_all', 'allow_unverified'):
            enabled = payload.get('enabled')
            if type(enabled) is not bool:
                raise ValueError('Choose whether to allow catch-all recipients.')
            with self.lock, self.connect() as db:
                db.execute("UPDATE settings SET value=? WHERE key=?", ('1' if enabled else '0', action))
                self.hold_unverified(db)
                self.assign_pending(db, [self.config(key) for key in json.loads(self.settings(db)['senders'])])
            return {action: enabled}
        if action == 'bounce':
            return {'bounced': self.mark_bounced(str(payload.get('email', '')).strip().lower())}
        if action == 'check_bounces':
            found, errors = 0, []
            for key in ('primary', 'second', 'third'):
                account = self.config(key)
                if not self.valid_config(account):
                    continue
                try:
                    for address in gmail_bounces(account, self.now()):
                        found += self.mark_bounced(address, account['sender'])
                except Exception:
                    errors.append(account['sender'])
            return {'bounced': found, 'scan_errors': errors}
        with self.lock, self.connect() as db:
            if action == 'start':
                self.hold_unverified(db)
                selected = payload.get('senders', ['primary'])
                if (not isinstance(selected, list) or not selected or len(selected) > 3
                        or any(not isinstance(key, str) or key not in ('primary', 'second', 'third') for key in selected)
                        or len(set(selected)) != len(selected)):
                    raise ValueError('Select one to three sender accounts.')
                accounts = [self.config(key) for key in selected]
                if not all(self.valid_config(c) for c in accounts):
                    raise ValueError('Add a separate app password for each selected mailbox in .env, then restart the server.')
                if len({c['sender'] for c in accounts}) != len(accounts):
                    raise ValueError('Each sender account must use a different email address.')
                limit = payload.get('daily_limit', DAILY_MAX)
                interval = payload.get('interval_minutes', 2)
                if type(limit) is not int or not 1 <= limit <= DAILY_MAX:
                    raise ValueError('Daily total must be between 1 and 1000 across selected accounts.')
                if type(interval) is not int or not 1 <= interval <= 60:
                    raise ValueError('Interval must be between 1 and 60 minutes.')
                db.execute("UPDATE settings SET value=? WHERE key='daily_limit'", (str(limit),))
                db.execute("UPDATE settings SET value=? WHERE key='interval_minutes'", (str(interval),))
                db.execute("UPDATE settings SET value=? WHERE key='senders'", (json.dumps(selected),))
                # An explicit Start action (including a changed interval or a
                # newly selected account) intentionally recalculates timing.
                db.execute("UPDATE settings SET value='0' WHERE key='next_send'")
                self.assign_pending(db, accounts)
                self.refresh_next_send(db)
                db.execute("UPDATE settings SET value='0' WHERE key='paused'")
            elif action == 'pause':
                db.execute("UPDATE settings SET value='1' WHERE key='paused'")
            elif action == 'cancel':
                db.execute("UPDATE messages SET status='cancelled' WHERE status IN ('pending','held')")
            elif action == 'suppress':
                email = str(payload.get('email', '')).strip().lower()
                if not EMAIL.fullmatch(email):
                    raise ValueError('Enter a valid email to block.')
                db.execute('INSERT OR IGNORE INTO suppressed VALUES (?)', (email,))
                db.execute("UPDATE messages SET status='cancelled' WHERE recipient=? AND status IN ('pending','held')", (email,))
            else:
                raise ValueError('Unknown action.')
        return {'ok': True}

    def pending_for_sender(self, db, email, priority=None):
        query = "SELECT recipient_timezone FROM messages WHERE status='pending' AND (planned_sender='' OR planned_sender=?)"
        values = [email]
        if priority is not None:
            query += ' AND priority=?'
            values.append(priority)
        schedule = json.loads(self.settings(db)['schedule'])
        now = self.now()
        return any(next_window(now, row[0], schedule) <= now for row in db.execute(query, values))

    def campaign_due(self, db, now):
        settings = self.settings(db)
        due = max(now, float(settings['next_send']))
        recent = db.execute('SELECT attempted FROM messages WHERE attempted>? ORDER BY attempted', (now - 86400,)).fetchall()
        limit = int(settings['daily_limit'])
        if len(recent) >= limit:
            due = max(due, recent[-limit][0] + 86400)
        return due

    def recipient_due(self, db, message, after, schedule):
        sender = message.get('planned_sender')
        if sender:
            after = max(after, self.sender_due(db, sender, int(self.settings(db)['interval_minutes']) * 60, self.now()))
        return next_window(after, message.get('recipient_timezone', ''), schedule)

    def assign_pending(self, db, accounts):
        rows = db.execute("SELECT id FROM messages WHERE status='pending' ORDER BY priority, created, id").fetchall()
        # Start with the mailbox that has sent the fewest emails in the
        # current rolling day. This avoids assigning a new high-priority
        # message to an account already at its daily ceiling.
        now = self.now()
        usage = {account['sender']: db.execute(
            'SELECT COUNT(*) FROM messages WHERE sender=? AND attempted>?',
            (account['sender'], now - 86400)).fetchone()[0] for account in accounts}
        accounts = sorted(accounts, key=lambda account: usage[account['sender']])
        available = [account for account in accounts if usage[account['sender']] < SENDER_DAILY_MAX] or accounts
        for index, (message_id,) in enumerate(rows):
            account = available[index % len(available)]
            db.execute("UPDATE messages SET planned_sender=? WHERE id=?", (account['sender'], message_id))

    def sender_due(self, db, email, interval, now):
        last = db.execute('SELECT MAX(COALESCE(sent, attempted)) FROM messages WHERE sender=?',
                          (email,)).fetchone()[0]
        due = (last + interval) if last is not None else 0
        attempts = db.execute('SELECT attempted FROM messages WHERE sender=? AND attempted>? ORDER BY attempted',
                              (email, now - 86400)).fetchall()
        if len(attempts) >= SENDER_DAILY_MAX:
            due = max(due, attempts[-SENDER_DAILY_MAX][0] + 86400)
        return due

    def refresh_next_send(self, db):
        s = self.settings(db)
        self.normalize_daily_limit(db)
        now = self.now()
        schedule = json.loads(s['schedule'])
        if schedule['enabled']:
            # The shared pause is a send-spacing constraint, not a saved window;
            # calculate windows anew so one sleeping zone cannot block another.
            last = db.execute('SELECT MAX(attempted) FROM messages').fetchone()[0]
            after = max(now, (last + int(s['interval_minutes']) * 60) if last is not None else now)
            recent = db.execute('SELECT attempted FROM messages WHERE attempted>? ORDER BY attempted', (now - 86400,)).fetchall()
            limit = int(s['daily_limit'])
            if len(recent) >= limit:
                after = max(after, recent[-limit][0] + 86400)
            rows = db.execute("SELECT planned_sender,recipient_timezone FROM messages WHERE status='pending'").fetchall()
            due = min((self.recipient_due(db, {'planned_sender': row[0], 'recipient_timezone': row[1]}, after, schedule) for row in rows), default=now)
            db.execute("UPDATE settings SET value=? WHERE key='next_send'", (str(due),))
            return due
        interval = int(s['interval_minutes']) * 60
        due_times = [self.sender_due(db, self.config(key)['sender'], interval, now)
                     for key in json.loads(s['senders'])
                     if self.pending_for_sender(db, self.config(key)['sender'])]
        # Start a batch only when every mailbox with a planned recipient is
        # ready.  tick() sends at most one message from each mailbox in that
        # batch, then their individual intervals create the next pause.
        due = max(due_times) if due_times else now
        # Preserve a shared batch pause set by the previous worker pass. This
        # matters when the next priority tier is assigned only to a mailbox
        # that did not send in that pass.
        prior_due = float(s.get('next_send', '0'))
        if prior_due > now:
            due = max(due, prior_due)
        recent = db.execute('SELECT attempted FROM messages WHERE attempted>? ORDER BY attempted',
                            (now - 86400,)).fetchall()
        limit = int(s['daily_limit'])
        if len(recent) >= limit:
            due = max(due, recent[-limit][0] + 86400)
        db.execute("UPDATE settings SET value=? WHERE key='next_send'", (str(due),))
        return due

    def tick(self):
        # One distinct pending recipient per due mailbox, up to three in this pass.
        # Keep a batch to one priority tier: guesses never share a batch with
        # remaining checked or public-address messages.
        with self.lock, self.connect() as db:
            self.hold_unverified(db)
            schedule = json.loads(self.settings(db)['schedule'])
            now = self.now()
            eligible = [row[0] for row in db.execute("SELECT priority,recipient_timezone FROM messages WHERE status='pending'") if next_window(now, row[1], schedule) <= now]
            priority = min(eligible) if eligible else None
            if priority is None and schedule['enabled']:
                self.refresh_next_send(db)
        if priority is None:
            return False
        # Do not catch up missed minutes with a burst after sleep/restart.
        used = set()
        for _ in range(3):
            sender = self.send_due(used, priority)
            if sender is None:
                break
            used.add(sender)
        return bool(used)

    def send_due(self, excluded, priority=None):
        # Lock covers SMTP: pause/suppress take effect before the next send begins.
        with self.lock:
            now = self.now()
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                s = self.settings(db)
                if s['paused'] == '1':
                    return None
                self.hold_unverified(db)
                accounts = [self.config(key) for key in json.loads(s['senders'])]
                if not all(self.valid_config(c) for c in accounts):
                    db.execute("UPDATE settings SET value='1' WHERE key='paused'")
                    return None
                # Only the first send in a batch waits for the shared batch
                # time.  Further sends in this same pass must not be held by
                # a following message already planned for a mailbox that sent.
                if not excluded and now < self.refresh_next_send(db):
                    return None
                # `tick()` can send several mailboxes in one batch. Enforce
                # the campaign-wide rolling limit here as well, not only when
                # calculating the next batch time.
                limit = self.normalize_daily_limit(db)
                recent = db.execute('SELECT COUNT(*) FROM messages WHERE attempted>?', (now - 86400,)).fetchone()[0]
                if recent >= limit:
                    self.refresh_next_send(db)
                    return None
                interval = int(s['interval_minutes']) * 60
                eligible = [(self.sender_due(db, c['sender'], interval, now), c['id'])
                            for c in accounts if c['sender'] not in excluded
                            and self.pending_for_sender(db, c['sender'], priority)]
                eligible = [(due, key) for due, key in eligible if due <= now]
                if not eligible:
                    return None
                sender_id = min(eligible)[1]
                account = self.config(sender_id)
                db.row_factory = sqlite3.Row
                query = "SELECT * FROM messages WHERE status='pending' AND (planned_sender='' OR planned_sender=?)"
                values = [account['sender']]
                if priority is not None:
                    query += ' AND priority=?'
                    values.append(priority)
                schedule = json.loads(s['schedule'])
                row = next((candidate for candidate in db.execute(query + ' ORDER BY priority, created, id', values) if next_window(self.now(), candidate['recipient_timezone'], schedule) <= self.now()), None)
                if not row:
                    return None
                row = dict(row)
                variants = json.loads(row['variants'])
                if variants:
                    used = db.execute("SELECT COUNT(*) FROM messages WHERE sender=? AND template IN ('A','B') AND attempted IS NOT NULL", (account['sender'],)).fetchone()[0]
                    choice = used % 2
                    row.update(variants[choice], template='AB'[choice])
                    db.execute('UPDATE messages SET subject=?,body=?,template=? WHERE id=?',
                               (row['subject'], row['body'], row['template'], row['id']))
                db.execute("UPDATE messages SET status='sending', attempted=?, sender=? WHERE id=?",
                           (now, account['sender'], row['id']))
                next_due = max(self.refresh_next_send(db), now + interval)
                db.execute("UPDATE settings SET value=? WHERE key='next_send'", (str(next_due),))
                row = dict(row)
                row['sender_id'], row['sender'] = sender_id, account['sender']
            try:
                self.transport(row)
            except Exception as error:
                # SMTP errors can include sensitive server text. Keep the UI error generic.
                label = 'failed' if isinstance(error, smtplib.SMTPResponseException) else 'uncertain'
                with self.connect() as db:
                    db.execute('UPDATE messages SET status=?, error=? WHERE id=?',
                               (label, 'SMTP send failed. Check mailbox and SMTP settings; queue paused. No automatic retry.', row['id']))
                    db.execute("UPDATE settings SET value='1' WHERE key='paused'")
                    self.refresh_next_send(db)
            else:
                with self.connect() as db:
                    db.execute("UPDATE messages SET status='sent', sent=? WHERE id=?", (self.now(), row['id']))
                    self.refresh_next_send(db)
            return account['sender']

    def send_smtp(self, row):
        c = self.config(row.get('sender_id', 'primary'))
        msg = EmailMessage()
        # HeaderRegistry handles display-name escaping and rejects newline injection.
        from email.headerregistry import Address
        msg['From'] = Address(display_name=c['name'], addr_spec=c['sender'])
        msg['To'] = row['recipient']
        msg['Subject'] = row['subject']
        msg['Date'] = formatdate(localtime=True)
        msg['Message-ID'] = make_msgid(domain=c['sender'].split('@')[-1])
        msg.set_content(row['body'] + '\n\nIf you would prefer no further emails from me, please reply and let me know.\n')
        context = ssl.create_default_context()
        smtp = (smtplib.SMTP_SSL(c['host'], c['port'], timeout=30, context=context)
                if c['security'] == 'ssl' else smtplib.SMTP(c['host'], c['port'], timeout=30))
        try:
            if c['security'] == 'starttls':
                smtp.starttls(context=context)
            smtp.login(c['user'], c['password'])
            refused = smtp.send_message(msg)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        finally:
            # Failure closing an already accepted message must not change its sent status.
            try:
                smtp.quit()
            except Exception:
                smtp.close()

    def run(self):
        while not self.stop_event.wait(2):
            try:
                self.tick()
            except Exception:
                with self.connect() as db:
                    db.execute("UPDATE settings SET value='1' WHERE key='paused'")

    def start_worker(self):
        thread = threading.Thread(target=self.run, daemon=True, name='outreach-mail')
        thread.start()
        return thread
