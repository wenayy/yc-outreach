import unittest

from public_contacts import discover, public_url


class PublishedContactsTests(unittest.TestCase):
    def test_linked_github_profile_adds_work_email_with_evidence(self):
        pages = {'https://company.test/': '',
                 'https://api.github.com/users/alice': '{"type":"User","name":"Alice Smith","email":"alice@company.test"}'}
        result = discover('https://company.test', 'company.test',
                          [{'name': 'Alice Smith', 'github': 'https://github.com/alice'}],
                          reader=lambda url, domain: (url, pages.get(url, ''), ''))
        self.assertEqual(result['contacts'][0]['email'], 'alice@company.test')
        self.assertEqual(result['contacts'][0]['source_platform'], 'GitHub')
        self.assertEqual(result['contacts'][0]['source_urls'], ['https://github.com/alice'])

    def test_github_wrong_identity_or_personal_email_not_added(self):
        for name, email in [('Someone Else', 'alice@company.test'), ('Alice Smith', 'alice@gmail.com')]:
            import json
            body = json.dumps({'type': 'User', 'name': name, 'email': email})
            result = discover('https://company.test', 'company.test', [{'name': 'Alice Smith', 'github': 'https://github.com/alice'}],
                              reader=lambda url, domain: (url, body if domain == 'api.github.com' else '', ''))
            self.assertEqual(result['contacts'], [])

    def test_company_team_records_add_new_people_and_linkedin_sources(self):
        pages = {'https://company.test/team': '<script type="application/ld+json">{"@type":"Person","name":"Bob Jones","jobTitle":"Head of Engineering","sameAs":"https://www.linkedin.com/in/bobjones"}</script>',
                 'https://www.linkedin.com/in/bobjones': '<p>Bob Jones · bob@company.test</p>'}
        result = discover('https://company.test', 'company.test', [], reader=lambda url, domain: (url, pages.get(url, ''), ''))
        self.assertEqual(result['contacts'][0]['name'], 'Bob Jones')
        self.assertEqual(result['contacts'][0]['source_platform'], 'LinkedIn')

    def test_named_team_link_discovers_new_github_person(self):
        pages = {'https://company.test/team': '<a href="https://github.com/bobjones">Bob Jones</a>',
                 'https://api.github.com/users/bobjones': '{"type":"User","name":"Bob Jones","email":"bob@company.test"}'}
        result = discover('https://company.test', 'company.test', [], reader=lambda url, domain: (url, pages.get(url, ''), ''))
        self.assertEqual(result['contacts'][0]['name'], 'Bob Jones')
        self.assertIn('https://company.test/team', result['contacts'][0]['source_urls'])

    def test_follows_team_link_and_keeps_provenance(self):
        pages = {'https://company.test/': '<a href="/our-team">People</a>',
                 'https://company.test/our-team': 'alice@company.test info@company.test bob [at] company [dot] test'}
        result = discover('https://company.test', 'company.test',
                          [{'name': 'Alice Smith'}, {'name': 'Bob Jones'}],
                          reader=lambda url, domain: (url, pages.get(url, ''), ''))
        self.assertEqual({c['email'] for c in result['contacts']}, {'alice@company.test', 'bob@company.test'})
        self.assertEqual(result['contacts'][0]['source_urls'], ['https://company.test/our-team'])
        self.assertEqual(result['general_emails'][0]['email'], 'info@company.test')
        self.assertLessEqual(len(result['pages']), 8)

    def test_ambiguous_first_name_is_not_assigned(self):
        result = discover('https://company.test', 'company.test',
                          [{'name': 'Alice Smith'}, {'name': 'Alice Jones'}],
                          reader=lambda url, domain: (url, 'alice@company.test outsider@other.test', ''))
        self.assertEqual(result['contacts'], [])

    def test_private_and_external_urls_rejected(self):
        for url, domain in [('http://127.0.0.1/', '127.0.0.1'),
                            ('https://other.test/', 'company.test'),
                            ('file:///etc/passwd', 'company.test')]:
            with self.assertRaises(ValueError):
                public_url(url, domain)
