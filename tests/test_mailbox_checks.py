import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mailbox_checks import CHECK_TTL, hard_bounce_recipients, import_run, mailbox_verdict
from outreach import MailQueue


def draft(address, source='site', company='Acme'):
    return dict(company=company, recipient=address, source=source, subject='Hello', body='Hi there.', allow_unverified=True)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000000
        self.sent = []
        self.env = patch.dict(os.environ, {'SMTP_HOST':'smtp.invalid', 'SMTP_USER':'sender@acme.com', 'SMTP_FROM':'sender@acme.com', 'SMTP_PASSWORD':'fixture'})
        self.env.start()
        self.q = MailQueue(Path(self.temp.name)/'queue.sqlite3', transport=self.sent.append, now=lambda:self.now)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_unverified_opt_in_releases_guesses_but_not_rejected_addresses(self):
        self.q.enqueue([draft('guess@acme.com', 'guess'), draft('bad@acme.com', 'guess'), draft('risk@acme.com', 'site')])
        self.q.apply_checks([('bad@acme.com','invalid',self.now,'fixture'), ('risk@acme.com','risky',self.now,'fixture')])
        self.q.action({'action':'allow_unverified','enabled':True})
        states = {m['recipient']:m['status'] for m in self.q.status()['messages']}
        self.assertEqual(states, {'guess@acme.com':'pending','bad@acme.com':'invalid','risk@acme.com':'held'})
        self.q.action({'action':'start'})
        self.assertTrue(self.q.tick())
        self.assertEqual([m['recipient'] for m in self.sent], ['guess@acme.com'])

    def test_claimed_checked_and_manual_addresses_cannot_bypass_mailbox_gate(self):
        for source in ('valid', 'verified', 'manual'):
            self.q.enqueue([draft(source+'@acme.com', source)])
        self.q.action({'action':'start'})
        self.assertFalse(self.q.tick())
        self.assertEqual({m['status'] for m in self.q.status()['messages']}, {'held'})

    def test_positive_check_releases_only_deliverable_nonblocked_draft(self):
        self.q.enqueue([draft('good@acme.com'), draft('risky@acme.com'), draft('blocked@acme.com')])
        self.q.action({'action':'suppress', 'email':'blocked@acme.com'})
        self.q.apply_checks([('good@acme.com','deliverable',self.now,'fixture'), ('risky@acme.com','catch-all',self.now,'fixture'), ('blocked@acme.com','deliverable',self.now,'fixture')])
        self.q.action({'action':'start'})
        self.q.tick()
        self.assertEqual([m['recipient'] for m in self.sent], ['good@acme.com'])
        self.assertEqual({m['recipient']:m['status'] for m in self.q.status()['messages']}, {'good@acme.com':'sent','risky@acme.com':'held','blocked@acme.com':'cancelled'})

    def test_evidence_expires_before_send_and_invalid_is_excluded(self):
        self.q.enqueue([draft('old@acme.com'), draft('bad@acme.com')])
        self.q.apply_checks([('old@acme.com','deliverable',self.now,'fixture'), ('bad@acme.com','invalid',self.now,'fixture')])
        self.q.action({'action':'start'})
        self.now += CHECK_TTL + 1
        self.assertFalse(self.q.tick())
        self.assertEqual({m['recipient']:m['status'] for m in self.q.status()['messages']}, {'old@acme.com':'held','bad@acme.com':'invalid'})

    def test_three_company_contacts_and_bounced_address_never_retried(self):
        self.assertEqual(self.q.enqueue([draft(f'p{i}@acme.com') for i in range(4)])['added'],3)
        self.q.apply_checks([('p0@acme.com','deliverable',self.now,'fixture')])
        self.q.action({'action':'start'})
        self.q.tick()
        self.q.action({'action':'bounce','email':'p0@acme.com'})
        self.assertEqual(self.q.status()['messages'][-1]['status'],'bounced')
        self.assertEqual(self.q.enqueue([draft('p0@acme.com')])['added'],0)
        self.assertEqual(self.q.enqueue([draft('replacement@acme.com')])['added'],1)

    def test_priority_batch_does_not_mix_guesses_with_checked_addresses(self):
        with patch.dict(os.environ, {'SMTP_2_USER':'second@gmail.com', 'SMTP_2_PASSWORD':'fixture'}):
            self.q.enqueue([draft('guess@acme.com','guess'), draft('checked@acme.com','valid')])
            self.q.apply_checks([(e,'deliverable',self.now,'fixture') for e in ('guess@acme.com','checked@acme.com')])
            self.q.action({'action':'start','senders':['primary','second']})
            self.q.tick()
            self.assertEqual([m['recipient'] for m in self.sent],['checked@acme.com'])
            self.assertFalse(self.q.tick())
            self.now += 120
            self.q.tick()
            self.assertEqual([m['recipient'] for m in self.sent],['checked@acme.com','guess@acme.com'])


    def test_templates_alternate_per_actual_sender_and_survive_restart(self):
        with patch.dict(os.environ, {'SMTP_2_USER':'second@gmail.com','SMTP_2_PASSWORD':'fixture'}):
            variants=[{'subject':'A subject','body':'A reviewed body'}, {'subject':'B subject','body':'B reviewed body'}]
            messages=[dict(draft(f'person{i}@acme.com',company=f'Company {i}'),variants=variants) for i in range(6)]
            self.q.enqueue([dict(draft('held@acme.com',company='Held'),variants=variants),*messages])
            self.q.apply_checks([(m['recipient'],'deliverable',self.now,'fixture') for m in messages])
            self.q.action({'action':'start','senders':['primary','second']})
            self.q.tick()
            self.assertEqual([m['template'] for m in self.sent],['A','A'])
            self.q=MailQueue(Path(self.temp.name)/'queue.sqlite3',transport=self.sent.append,now=lambda:self.now)
            self.assertTrue(self.q.status()['paused'])
            self.now+=120
            self.q.action({'action':'start','senders':['primary','second']})
            self.q.tick()
            self.now+=120
            self.q.tick()
            for sender in ('sender@acme.com','second@gmail.com'):
                rows=[m for m in self.sent if m['sender']==sender]
                self.assertEqual([m['template'] for m in rows],['A','B','A'])
                self.assertEqual([m['body'] for m in rows],['A reviewed body','B reviewed body','A reviewed body'])
            self.assertEqual(next(m for m in self.q.status()['messages'] if m['recipient']=='held@acme.com')['status'],'held')

    def test_unfinished_second_template_rejects_whole_enqueue(self):
        with self.assertRaises(ValueError):
            self.q.enqueue([dict(draft('jane@acme.com'),variants=[{'subject':'A','body':'Hi'}, {'subject':'B','body':'{company}'}])])
        self.assertEqual(self.q.status()['messages'],[])

    def test_daily_total_can_be_1000_without_starting(self):
        self.q.action({'action':'set_daily_limit','daily_limit':1000})
        self.assertEqual(self.q.status()['daily_limit'],1000)
        self.assertTrue(self.q.status()['paused'])
        self.q.action({'action':'start','daily_limit':1000,'senders':['primary']})
        with self.assertRaises(ValueError):
            self.q.action({'action':'start','daily_limit':1001})

    def test_legacy_daily_limit_is_upgraded_to_1000(self):
        with self.q.connect() as db:
            db.execute("UPDATE settings SET value='25' WHERE key='daily_limit'")
        self.assertEqual(self.q.status()['daily_limit'], 1000)
        with self.q.connect() as db:
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='daily_limit'").fetchone()[0], '1000')

    def test_catch_all_opt_in_keeps_unknown_blocked_and_verified_first(self):
        self.q.enqueue([draft('catch@acme.com','site'), draft('good@acme.com','guess'), draft('unknown@acme.com')])
        self.q.apply_checks([('catch@acme.com','catch-all',self.now,'fixture'), ('good@acme.com','deliverable',self.now,'fixture'), ('unknown@acme.com','unknown',self.now,'fixture')])
        self.q.action({'action':'allow_catch_all','enabled':True})
        self.q.action({'action':'start'})
        self.q.tick()
        self.assertEqual([m['recipient'] for m in self.sent],['good@acme.com'])
        self.q.action({'action':'allow_catch_all','enabled':False})
        self.now+=120
        self.assertFalse(self.q.tick())
        self.q.action({'action':'allow_catch_all','enabled':True})
        self.q.tick()
        self.assertEqual([m['recipient'] for m in self.sent],['good@acme.com','catch@acme.com'])
        self.assertEqual(next(m for m in self.q.status()['messages'] if m['recipient']=='unknown@acme.com')['status'],'held')


class EvidenceTests(unittest.TestCase):
    def valid(self):
        return dict(email='jane@acme.com',smtpStatus='valid',isValid=True,isCatchAll=False,isDisposable=False,isValidSyntax=True,hasMxRecords=True,smtpCode=250)

    def test_missing_signals_catch_all_and_dns_only_are_never_deliverable(self):
        self.assertEqual(mailbox_verdict(self.valid()),'deliverable')
        for changes in ({'isCatchAll':True}, {'smtpCode':None}, {'isValid':False}, {'smtpStatus':'unknown'}, {'isDisposable':True}):
            row=self.valid(); row.update(changes)
            self.assertNotEqual(mailbox_verdict(row),'deliverable')
        self.assertEqual(mailbox_verdict({'email':'jane@acme.com','status':'valid','is_valid':True}),'unknown')

    def bounce_valid(self):
        return dict(email='jane@acme.com',status='valid',smtp_valid=True,is_catch_all=False,
                    is_disposable=False,syntax_valid=True,domain_exists=True,mx_found=True,risk_type=None)

    def test_bounceverify_requires_explicit_mailbox_and_risk_signals(self):
        self.assertEqual(mailbox_verdict(self.bounce_valid(),'bounceverify'),'deliverable')
        self.assertEqual(mailbox_verdict(dict(self.bounce_valid(),risk_type='none'),'bounceverify'),'deliverable')
        for field in ('smtp_valid','is_catch_all','is_disposable','syntax_valid','domain_exists','mx_found'):
            row=self.bounce_valid(); row.pop(field)
            self.assertNotEqual(mailbox_verdict(row,'bounceverify'),'deliverable')
        for changes in ({'smtp_valid':False}, {'status':'unknown'}, {'is_catch_all':True},
                        {'is_disposable':True}, {'risk_type':'risky'}, {'error':'timeout'}):
            row=self.bounce_valid(); row.update(changes)
            self.assertNotEqual(mailbox_verdict(row,'bounceverify'),'deliverable')
        self.assertEqual(mailbox_verdict({'status':'valid','score':100},'bounceverify'),'unknown')
        self.assertEqual(mailbox_verdict({'status':'invalid'},'bounceverify'),'invalid')

    def test_bounceverify_import_checks_actor_and_requested_addresses(self):
        now=1000000
        run=dict(actId='Bounce1',status='SUCCEEDED',finishedAt=dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat(),defaultDatasetId='Data1',defaultKeyValueStoreId='Input1')
        input_row={'emails':['JANE@acme.com']}
        def fetch(path, token):
            if path.startswith('acts/'):
                self.assertEqual(path,'acts/bounceverify~bounceverify-email-verifier')
                return {'data':{'id':'Bounce1'}}
            if path.startswith('actor-runs/'): return {'data':run}
            if path.startswith('key-value-stores/'): return input_row
            return [self.bounce_valid(), dict(self.bounce_valid(),email='unrequested@acme.com')]
        result=import_run('Run1','fixture',now,fetch,provider='bounceverify')
        self.assertEqual([(r[0],r[1]) for r in result],[('jane@acme.com','deliverable')])
        run['actId']='DomainOnly1'
        with self.assertRaises(ValueError): import_run('Run1','fixture',now,fetch,provider='bounceverify')
        run['actId']='Bounce1'; input_row['emails']=[]
        with self.assertRaises(ValueError): import_run('Run1','fixture',now,fetch,provider='bounceverify')
        with self.assertRaises(ValueError): import_run('Run1','fixture',now,fetch,provider='arbitrary')

    def test_import_rejects_wrong_actor_stale_runs_and_disabled_catch_all(self):
        now=1000000
        run=dict(actId='Actor1',status='SUCCEEDED',finishedAt=dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat(),defaultDatasetId='Data1',defaultKeyValueStoreId='Input1')
        input_row={'catchAllTest':True}
        def fetch(path, token):
            if path.startswith('acts/'): return {'data':{'id':'Actor1'}}
            if path.startswith('actor-runs/'): return {'data':run}
            if path.startswith('key-value-stores/'): return input_row
            return [self.valid()]
        self.assertEqual(import_run('Run1','fixture',now,fetch)[0][1],'deliverable')
        run['actId']='WrongActor'
        with self.assertRaises(ValueError): import_run('Run1','fixture',now,fetch)
        run['actId']='Actor1'; input_row['catchAllTest']=False
        with self.assertRaises(ValueError): import_run('Run1','fixture',now,fetch)
        input_row['catchAllTest']=True
        with self.assertRaises(ValueError): import_run('Run1','fixture',now+CHECK_TTL+1,fetch)

    def test_only_structured_hard_bounce_is_a_bounce(self):
        raw=b'''MIME-Version: 1.0\nContent-Type: multipart/report; report-type=delivery-status; boundary="b"\n\n--b\nContent-Type: text/plain\n\nDelivery failed\n--b\nContent-Type: message/delivery-status\n\nReporting-MTA: dns; mx.example.com\n\nFinal-Recipient: rfc822; jane@acme.com\nAction: failed\nStatus: 5.1.1\n\n--b--\n'''
        self.assertEqual(hard_bounce_recipients(raw),{'jane@acme.com'})
        self.assertEqual(hard_bounce_recipients(raw.replace(b'5.1.1',b'4.2.0')),set())
        self.assertEqual(hard_bounce_recipients(b'Subject: Re: Delivery failed\n\nAddress not found jane@acme.com'),set())
