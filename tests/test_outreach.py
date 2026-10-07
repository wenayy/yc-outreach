import json
import os
import smtplib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from contact_emails import extract_emails, founder_match, founder_emails
from outreach import MailQueue, INTERVAL
from serve import Handler

CONFIG = {'SMTP_HOST': 'smtp.test.invalid', 'SMTP_USER': 'sender@acme.com',
          'SMTP_PASSWORD': 'test-password', 'SMTP_FROM': 'sender@acme.com'}


def message(email='jane@acme.com', **extra):
    return dict(company='Acme ' + email, recipient=email, subject='A role at Acme',
                body='Hi Jane, I built a project relevant to Acme.', source='site', **extra)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = 1000000.0
        self.sent = []
        self.path = Path(self.temp.name) / 'queue.sqlite3'
        self.q = MailQueue(self.path, transport=self.sent.append, now=lambda: self.clock)
        self.env = patch.dict(os.environ, CONFIG)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def enqueue_verified(self, messages):
        # Scheduler tests explicitly provide the prerequisite mailbox evidence.
        self.q.apply_checks([(m.get('recipient', '').lower(), 'deliverable', self.clock, 'fixture') for m in messages])
        return self.q.enqueue(messages)

    def start(self, limit=20):
        self.q.action({'action': 'start', 'daily_limit': limit})

    def test_pause_spacing_and_no_duplicate_send_after_reload(self):
        self.assertEqual(self.enqueue_verified([message(), message(), message('jo@acme.com')]), {'added': 2, 'skipped': 1})
        self.assertFalse(self.q.tick())
        self.start()
        self.assertTrue(self.q.tick())
        self.clock += INTERVAL - 1
        self.assertFalse(self.q.tick())
        self.clock += 1
        self.assertTrue(self.q.tick())
        self.assertEqual(len(self.sent), 2)
        reloaded = MailQueue(self.path, transport=self.sent.append, now=lambda: self.clock)
        self.assertTrue(reloaded.status()['paused'])
        self.assertEqual(reloaded.enqueue([message()])['added'], 0)

    def test_discovered_address_cannot_duplicate_queued_or_sent_recipient(self):
        self.enqueue_verified([message('jane@acme.com')])
        rediscovered = message(' JANE@ACME.COM ')
        rediscovered['company'] = 'Same company, rediscovered'
        self.assertEqual(self.q.enqueue([rediscovered]), {'added': 0, 'skipped': 1})
        self.start()
        self.q.tick()
        self.assertEqual(self.q.enqueue([rediscovered]), {'added': 0, 'skipped': 1})
        self.assertEqual(len(self.sent), 1)

    def test_database_blocks_case_and_whitespace_duplicates(self):
        import sqlite3
        self.enqueue_verified([message('jane@acme.com')])
        with self.q.connect() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO messages(recipient) VALUES(?)", (' JANE@ACME.COM ',))

    def test_concurrent_sources_enqueue_same_recipient_only_once(self):
        results = []
        workers = [threading.Thread(target=lambda: results.append(self.q.enqueue([message(' JANE@ACME.COM ')]))) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(sum(result['added'] for result in results), 1)
        self.assertEqual(len(self.q.status()['messages']), 1)

    def test_rolling_daily_limit(self):
        self.enqueue_verified([message(), message('jo@acme.com')])
        self.start(limit=1)
        self.q.tick()
        self.clock += INTERVAL
        self.assertFalse(self.q.tick())
        self.clock += 86400 - INTERVAL
        self.assertTrue(self.q.tick())

    def test_failed_send_pauses_and_does_not_retry(self):
        def fail(row):
            raise smtplib.SMTPAuthenticationError(535, b'secret-text')
        self.q.transport = fail
        self.enqueue_verified([message()])
        self.start()
        self.q.tick()
        state = self.q.status()
        self.assertTrue(state['paused'])
        self.assertEqual(state['messages'][0]['status'], 'failed')
        self.assertNotIn('secret-text', json.dumps(state))
        self.q.transport = self.sent.append
        self.clock += INTERVAL
        self.start()
        self.assertFalse(self.q.tick())

    def test_crash_during_send_is_uncertain_and_not_retried(self):
        self.enqueue_verified([message()])
        with self.q.connect() as db:
            db.execute("UPDATE messages SET status='sending'")
        q = MailQueue(self.path, transport=self.sent.append, now=lambda: self.clock)
        self.assertEqual(q.status()['messages'][0]['status'], 'uncertain')
        self.assertTrue(q.status()['paused'])

    def test_suppression_cancels_pending_and_blocks_new_queue(self):
        self.enqueue_verified([message()])
        self.q.action({'action': 'suppress', 'email': 'Jane@acme.com'})
        self.assertEqual(self.q.status()['messages'][0]['status'], 'cancelled')
        self.assertEqual(self.enqueue_verified([message()])['added'], 0)
        self.start()
        self.assertFalse(self.q.tick())

    def test_cancel_allows_revised_draft_to_be_queued(self):
        self.enqueue_verified([message()])
        self.q.action({'action': 'cancel'})
        self.assertEqual(self.enqueue_verified([message()])['added'], 1)

    def test_rejects_guesses_header_injection_and_unfinished_templates_atomically(self):
        for changes in ({'source': 'guess'}, {'subject': 'hello\nBcc: x@acme.com'},
                        {'body': '[1-2 lines about you]'}, {'recipient': 'bad address'},
                        {'body': 'Hello {first_name}'}):
            bad = message()
            bad.update(changes)
            with self.assertRaises(ValueError):
                self.enqueue_verified([message('good@acme.com'), bad])
        self.assertEqual(self.q.status()['messages'], [])

    def test_unverified_addresses_require_explicit_acknowledgement(self):
        for source in ('guess', 'unknown'):
            item = message()
            item['source'] = source
            for ack in (None, False, 'true', 1):
                item['allow_unverified'] = ack
                with self.assertRaises(ValueError):
                    self.enqueue_verified([item])
        item = message()
        item.update(source='guess', allow_unverified=True)
        self.assertEqual(self.enqueue_verified([item])['added'], 1)
        with self.q.connect() as db:
            self.assertEqual(db.execute('SELECT source, unverified_ack FROM messages').fetchone(), ('guess', 1))
        self.start()
        self.assertTrue(self.q.tick())
        self.assertEqual(self.sent[0]['source'], 'guess')

    def test_spacing_is_measured_from_smtp_completion(self):
        def slow(row):
            self.clock += 30
            self.sent.append(row)
        self.q.transport = slow
        self.enqueue_verified([message(), message('jo@acme.com')])
        self.start()
        self.q.tick()
        self.clock += INTERVAL - 1
        self.assertFalse(self.q.tick())
        self.clock += 1
        self.assertTrue(self.q.tick())

    def test_smtp_uses_tls_and_quit_failure_does_not_mark_send_failed(self):
        self.enqueue_verified([message()])
        with patch('outreach.smtplib.SMTP') as smtp:
            smtp.return_value.send_message.return_value = {}
            smtp.return_value.quit.side_effect = OSError('connection closed')
            self.q.transport = self.q.send_smtp
            self.start()
            self.q.tick()
            smtp.return_value.starttls.assert_called_once()
            smtp.return_value.login.assert_called_once_with('sender@acme.com', 'test-password')
            msg = smtp.return_value.send_message.call_args.args[0]
            self.assertEqual(msg['To'], 'jane@acme.com')
            self.assertIn('please reply', msg.get_content())
            self.assertEqual(self.q.status()['messages'][0]['status'], 'sent')

    def test_three_accounts_send_distinct_recipients_each_minute(self):
        extra = {'SMTP_2_USER': 'second@gmail.com', 'SMTP_2_PASSWORD': 'second-password',
                 'SMTP_3_USER': 'third@gmail.com', 'SMTP_3_PASSWORD': 'third-password'}
        with patch.dict(os.environ, extra):
            self.enqueue_verified([message(f'person{i}@acme.com') for i in range(7)])
            self.q.action({'action': 'start', 'senders': ['primary', 'second', 'third'],
                           'daily_limit': 100, 'interval_minutes': 1})
            self.assertTrue(self.q.tick())
            self.assertEqual(len(self.sent), 3)
            self.assertFalse(self.q.tick())
            self.clock += 59
            self.assertFalse(self.q.tick())
            self.clock += 1
            self.assertTrue(self.q.tick())
            self.assertEqual(len(self.sent), 6)
            self.clock += 60
            self.assertTrue(self.q.tick())
            self.assertEqual(len(self.sent), 7)
            self.assertEqual(len({m['recipient'] for m in self.sent}), 7)
            self.assertEqual([m['sender'] for m in self.sent],
                             ['sender@acme.com', 'second@gmail.com', 'third@gmail.com'] * 2 + ['sender@acme.com'])
            state = self.q.status()
            self.assertEqual(state['interval_scope'], 'per_account')
            self.assertNotIn('second-password', json.dumps(state))
            self.assertEqual(state['messages'][0]['sender'], 'sender@acme.com')

    def test_batch_respects_remaining_total_daily_budget(self):
        with patch.dict(os.environ, {'SMTP_2_USER': 'second@gmail.com', 'SMTP_2_PASSWORD': 'pw',
                                      'SMTP_3_USER': 'third@gmail.com', 'SMTP_3_PASSWORD': 'pw'}):
            self.enqueue_verified([message(f'person{i}@acme.com') for i in range(5)])
            self.q.action({'action': 'start', 'senders': ['primary', 'second', 'third'], 'daily_limit': 2})
            self.q.tick()
            self.assertEqual(len(self.sent), 2)
            self.clock += 60
            self.assertFalse(self.q.tick())
            self.clock += 86400
            self.q.tick()
            self.assertEqual(len(self.sent), 4)

    def test_failure_stops_batch_without_assigning_other_recipients(self):
        def fail(row):
            raise smtplib.SMTPAuthenticationError(535, b'error')
        with patch.dict(os.environ, {'SMTP_2_USER': 'second@gmail.com', 'SMTP_2_PASSWORD': 'pw'}):
            self.q.transport = fail
            self.enqueue_verified([message(), message('other@acme.com')])
            self.q.action({'action': 'start', 'senders': ['primary', 'second']})
            self.q.tick()
            state = self.q.status()
            self.assertTrue(state['paused'])
            self.assertEqual([m['status'] for m in state['messages']], ['pending', 'failed'])
            self.assertEqual(state['messages'][0]['sender'], '')

    def test_new_account_can_send_while_primary_waits(self):
        with patch.dict(os.environ, {'SMTP_2_USER': 'second@gmail.com', 'SMTP_2_PASSWORD': 'pw'}):
            self.enqueue_verified([message(), message('other@acme.com')])
            self.start()
            self.q.tick()
            self.clock += 10
            self.q.action({'action': 'start', 'senders': ['primary', 'second']})
            self.q.tick()
            self.assertEqual([m['sender'] for m in self.sent], ['sender@acme.com', 'second@gmail.com'])

    def test_sleep_does_not_create_catch_up_burst(self):
        with patch.dict(os.environ, {'SMTP_2_USER': 'second@gmail.com', 'SMTP_2_PASSWORD': 'pw',
                                      'SMTP_3_USER': 'third@gmail.com', 'SMTP_3_PASSWORD': 'pw'}):
            self.enqueue_verified([message(f'person{i}@acme.com') for i in range(20)])
            self.q.action({'action': 'start', 'senders': ['primary', 'second', 'third']})
            self.q.tick()
            self.clock += 3600
            self.q.tick()
            self.assertEqual(len(self.sent), 6)
            self.assertFalse(self.q.tick())

    def test_sender_daily_cap_survives_restart(self):
        with patch.dict(os.environ, {'SMTP_2_USER': 'second@gmail.com', 'SMTP_2_PASSWORD': 'second-password'}):
            self.enqueue_verified([message(f'person{i}@acme.com') for i in range(501)])
            with self.q.connect() as db:
                db.execute("UPDATE messages SET status='sent', attempted=?, sent=?, sender='sender@acme.com' WHERE id<=500",
                           (self.clock - 120, self.clock - 120))
            self.q.action({'action': 'start', 'senders': ['primary', 'second'], 'daily_limit': 500})
            self.assertFalse(self.q.tick())
            self.clock += 86400
            self.q.apply_checks([('person500@acme.com','deliverable',self.clock,'fixture')])
            self.assertTrue(self.q.tick())

    def test_shared_1000_limit_uses_second_sender_after_first_reaches_500(self):
        with patch.dict(os.environ, {'SMTP_2_USER': 'second@gmail.com', 'SMTP_2_PASSWORD': 'second-password'}):
            self.enqueue_verified([message(f'person{i}@acme.com') for i in range(501)])
            with self.q.connect() as db:
                db.execute("UPDATE messages SET status='sent', attempted=?, sent=?, sender='sender@acme.com' WHERE id<=500", (self.clock-120,self.clock-120))
            self.q.action({'action':'start','senders':['primary','second'],'daily_limit':1000})
            self.assertTrue(self.q.tick())
            self.assertEqual(self.sent[-1]['sender'], 'second@gmail.com')
            self.assertEqual(self.q.status()['accounts'][0]['daily_limit'],500)

    def test_unconfigured_or_duplicate_senders_cannot_start(self):
        for selected in ([], ['unknown'], ['primary', 'primary'], ['second']):
            with self.assertRaises(ValueError):
                self.q.action({'action': 'start', 'senders': selected})
        with patch.dict(os.environ, {'SMTP_2_USER': 'sender@acme.com', 'SMTP_2_PASSWORD': 'pw'}):
            with self.assertRaises(ValueError):
                self.q.action({'action': 'start', 'senders': ['primary', 'second']})

    def test_shortening_interval_updates_existing_schedule(self):
        self.enqueue_verified([message(), message('jo@acme.com')])
        self.q.action({'action': 'start', 'interval_minutes': 20})
        self.q.tick()
        self.clock += 60
        self.q.action({'action': 'pause'})
        self.q.action({'action': 'start', 'interval_minutes': 1})
        self.assertTrue(self.q.tick())

    def test_smtp_uses_selected_accounts_own_credentials(self):
        with patch.dict(os.environ, {'SMTP_2_USER': 'second@gmail.com', 'SMTP_2_PASSWORD': 'second-password'}):
            with patch('outreach.smtplib.SMTP') as smtp:
                smtp.return_value.send_message.return_value = {}
                row = message()
                row['sender_id'] = 'second'
                self.q.send_smtp(row)
                smtp.return_value.login.assert_called_once_with('second@gmail.com', 'second-password')
                msg = smtp.return_value.send_message.call_args.args[0]
                self.assertEqual(msg['From'].addresses[0].addr_spec, 'second@gmail.com')



class DiscoveryTests(unittest.TestCase):
    def test_extracts_mailto_and_explicit_obfuscation_without_foreign_domains(self):
        body = 'mailto:jane%40acme.com Jane [at] acme [dot] com help@other.com icon@2x.png'
        self.assertEqual(extract_emails(body, 'acme.com'), ['jane@acme.com'])

    def test_shared_first_name_does_not_assign_one_mailbox_to_two_founders(self):
        emails = ['ann@acme.com', 'ann.smith@acme.com']
        names = ['Ann Smith', 'Ann Jones']
        self.assertEqual(founder_emails('Ann Smith', emails, names), ['ann.smith@acme.com'])
        self.assertEqual(founder_emails('Ann Jones', emails, names), [])

    def test_exact_founder_patterns_avoid_prefix_false_matches(self):
        self.assertFalse(founder_match('Ann Smith', 'anna@acme.com'))
        self.assertFalse(founder_match('Jane Doe', 'jane-support@acme.com'))
        self.assertTrue(founder_match('Jane Doe', 'jdoe@acme.com'))
        self.assertTrue(founder_match('Jane Doe', 'jane.doe@acme.com'))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        Handler.queue = MailQueue(Path(self.temp.name) / 'queue.sqlite3', transport=lambda row: None)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def test_local_status_and_authenticated_mutation(self):
        state = json.load(urllib.request.urlopen(self.base + '/api/mail'))
        req = urllib.request.Request(self.base + '/api/mail', data=json.dumps({'action': 'enqueue', 'messages': [message()]}).encode(),
                                     headers={'Content-Type': 'application/json', 'X-Outreach-Token': state['token']})
        self.assertEqual(json.load(urllib.request.urlopen(req))['added'], 1)

    def test_catalog_job_uses_authenticated_nonblocking_endpoint(self):
        from catalog_jobs import CatalogJobs
        seed=Path(self.temp.name)/'seed.json'
        seed.write_text('[]')
        release=threading.Event()
        Handler.catalog=CatalogJobs(Path(self.temp.name)/'catalog',seed,lambda:[{'batch':'Winter 2024'}],lambda _:release.wait(3) and [])
        try:
            denied=urllib.request.Request(self.base+'/api/catalog',data=b'{"action":"refresh"}',headers={'Content-Type':'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(denied)
            self.assertEqual(error.exception.code,403)
            error.exception.close()
            req=urllib.request.Request(self.base+'/api/catalog',data=b'{"action":"refresh"}',headers={'Content-Type':'application/json','X-Outreach-Token':Handler.queue.token})
            started=json.load(urllib.request.urlopen(req))
            status=json.load(urllib.request.urlopen(self.base+'/api/catalog'))
            self.assertTrue(status['running'])
            self.assertEqual(status['run_id'],started['run_id'])
            self.assertNotIn('baseline',status)
        finally:
            release.set()
            if Handler.catalog.thread:Handler.catalog.thread.join(3)
            Handler.catalog=None

    def test_denies_cross_origin_missing_token_and_private_files(self):
        requests = [
            urllib.request.Request(self.base + '/api/mail', headers={'Origin': 'https://evil.invalid'}),
            urllib.request.Request(self.base + '/api/mail', headers={'Host': 'evil.invalid'}),
            urllib.request.Request(self.base + '/api/mail', data=b'{"action":"pause"}', headers={'Content-Type': 'application/json'}),
        ]
        for req in requests:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(req)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        for path in ('/.env', '/.outreach/queue.sqlite3', '/.git/config', '/serve.py'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.base + path)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()


if __name__ == '__main__':
    unittest.main()
