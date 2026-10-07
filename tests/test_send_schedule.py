import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from outreach import MailQueue
from send_schedule import DEFAULT, validate, next_window


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.schedule = validate({**DEFAULT, 'enabled': True})

    def test_local_half_hour_zone_and_exclusive_end(self):
        now = stamp('2026-10-07T03:30:00')
        self.assertEqual(next_window(now, 'Asia/Kolkata', self.schedule), now)
        end = stamp('2026-10-07T05:30:00')
        self.assertEqual(next_window(end, 'Asia/Kolkata', self.schedule), stamp('2026-10-08T03:30:00'))

    def test_weekend_and_earliest_start(self):
        self.assertEqual(next_window(stamp('2026-10-09T15:00:00'), 'Europe/London', self.schedule), stamp('2026-10-12T08:00:00'))
        future = {**self.schedule, 'not_before': stamp('2026-10-08T09:30:00')}
        self.assertEqual(next_window(stamp('2026-10-07T08:00:00'), 'Europe/London', future), future['not_before'])

    def test_dst_changes_and_nonexistent_hour(self):
        self.assertEqual(next_window(stamp('2026-03-06T17:00:00'), 'America/New_York', self.schedule), stamp('2026-03-09T13:00:00'))
        gap = {**self.schedule, 'weekdays_only': False, 'start': '02:00', 'end': '04:00'}
        self.assertEqual(next_window(stamp('2026-03-08T06:00:00'), 'America/New_York', gap), stamp('2026-03-08T07:00:00'))

    def test_disabled_mode_unchanged_and_bad_settings_rejected(self):
        now = stamp('2026-10-10T20:00:00')
        self.assertEqual(next_window(now, '', DEFAULT), now)
        for changes in ({'start': '12:00', 'end': '09:00'}, {'fallback_timezone': 'Fake/Place'}, {'not_before': float('nan')}):
            with self.assertRaises(ValueError):
                validate({**DEFAULT, **changes})


class ScheduledQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = stamp('2026-10-07T08:00:00')
        self.sent = []
        self.path = Path(self.temp.name) / 'queue.sqlite3'
        self.env = patch.dict(os.environ, {'SMTP_HOST': 'smtp.test', 'SMTP_USER': 'sender@test.com', 'SMTP_PASSWORD': 'fixture', 'SMTP_FROM': 'sender@test.com'})
        self.env.start()
        self.q = MailQueue(self.path, transport=self.sent.append, now=lambda: self.clock)
        self.q.action({'action': 'set_schedule', 'schedule': {**DEFAULT, 'enabled': True}})

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def add(self, email, zone, source='site', verified=True):
        if verified:
            self.q.apply_checks([(email, 'deliverable', self.clock, 'fixture')])
        self.q.enqueue([{'company': email, 'recipient': email, 'source': source, 'subject': 'Hello', 'body': 'A reviewed message', 'recipient_timezone': zone}])

    def start(self, limit=1000):
        self.q.action({'action': 'start', 'senders': ['primary'], 'daily_limit': limit, 'interval_minutes': 2})

    def test_closed_high_priority_zone_does_not_block_open_zone(self):
        self.add('ny@company.com', 'America/New_York', 'verified')
        self.add('london@company.com', 'Europe/London')
        self.start()
        self.assertTrue(self.q.tick())
        self.assertEqual(self.sent[0]['recipient'], 'london@company.com')
        self.assertFalse(self.q.tick())
        self.clock = stamp('2026-10-07T13:00:00')
        self.assertTrue(self.q.tick())
        self.assertEqual(self.sent[1]['recipient'], 'ny@company.com')

    def test_interval_daily_limit_and_verification_still_apply(self):
        self.add('one@company.com', 'Europe/London')
        self.add('two@company.com', 'Europe/London')
        self.add('held@company.com', 'Europe/London', verified=False)
        self.start(limit=1)
        self.assertTrue(self.q.tick())
        self.clock += 120
        self.assertFalse(self.q.tick())
        rows = {row['recipient']: row for row in self.q.status()['messages']}
        self.assertEqual(rows['held@company.com']['status'], 'held')
        self.assertEqual(rows['two@company.com']['scheduled_for'], stamp('2026-10-08T08:00:00'))
        self.clock = stamp('2026-10-08T08:00:00')
        self.assertTrue(self.q.tick())
        self.assertEqual(len(self.sent), 2)

    def test_persistence_timezone_edit_and_no_duplicate(self):
        self.add('one@company.com', 'America/New_York')
        self.q.action({'action': 'recipient_timezone', 'recipient': 'one@company.com', 'recipient_timezone': 'Europe/London'})
        reloaded = MailQueue(self.path, transport=self.sent.append, now=lambda: self.clock)
        self.assertTrue(reloaded.status()['schedule']['enabled'])
        self.assertTrue(reloaded.status()['paused'])
        self.assertEqual(reloaded.status()['messages'][0]['recipient_timezone'], 'Europe/London')
        reloaded.action({'action': 'start', 'daily_limit': 1000})
        reloaded.tick()
        self.add('one@company.com', 'America/New_York')
        self.assertEqual(len(self.q.status()['messages']), 1)

    def test_fallback_and_future_not_before(self):
        self.q.action({'action': 'set_schedule', 'schedule': {**DEFAULT, 'enabled': True, 'fallback_timezone': 'Europe/London', 'not_before': stamp('2026-10-08T08:00:00')}})
        self.add('one@company.com', '')
        self.start()
        self.assertFalse(self.q.tick())
        self.clock = stamp('2026-10-08T08:00:00')
        self.assertTrue(self.q.tick())

    def test_waking_after_window_never_sends_catchup(self):
        self.add('one@company.com', 'Europe/London')
        self.start()
        self.clock = stamp('2026-10-07T10:00:00')
        self.assertFalse(self.q.tick())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.q.status()['messages'][0]['scheduled_for'], stamp('2026-10-08T08:00:00'))

    def test_location_suggestions_preserve_manual_overrides(self):
        self.add('manual@company.com', 'America/New_York')
        self.add('unknown@company.com', '')
        self.q.action({'action': 'set_schedule', 'schedule': {**DEFAULT, 'enabled': True},
                       'suggested_timezones': [{'recipient': email, 'recipient_timezone': 'Europe/London'} for email in ('manual@company.com', 'unknown@company.com')]})
        rows = {row['recipient']: row for row in self.q.status()['messages']}
        self.assertEqual(rows['manual@company.com']['recipient_timezone'], 'America/New_York')
        self.assertEqual(rows['unknown@company.com']['timezone_source'], 'company location')
