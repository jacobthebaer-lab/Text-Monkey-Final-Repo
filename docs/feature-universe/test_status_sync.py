"""Credential-free Git fixtures for the sanitized public feature-status feed."""
import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

MODULE = Path(__file__).resolve().parent / 'status_sync.py'
spec = importlib.util.spec_from_file_location('feature_status_sync', MODULE)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
NOW = datetime(2026, 10, 3, 19, 0, tzinfo=timezone.utc)


class StatusSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('remote', 'add', 'origin', sync.GITHUB + '.git')
        self.write('app/core.py', 'baseline\n')
        self.write('docs/readme.md', 'baseline\n')
        self.write('tests/fixture.txt', 'baseline\n')
        self.base = self.commit()
        self.model = {'revision': self.base, 'categories': [{'features': [
            {'id': 'core', 'status': 'partial', 'sources': ['app/core.py:12']},
            {'id': 'documentation', 'status': 'implemented', 'sources': ['docs/readme.md']},
            {'id': 'suite', 'status': 'planned', 'sources': ['tests/']},
            {'id': 'conversation', 'status': 'planned', 'sources': [{'chatId': 'synthetic-chat', 'title': 'Synthetic scope'}]},
        ]}]}

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def write(self, path, text):
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'Synthetic source revision')
        sha = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/codex/complete-text-monkey', sha)
        return sha

    def feed(self, reviews=None, pulls=None):
        return sync.build_feed(self.repo, sync.DEFAULT_REF, self.model, reviews, pulls, now=NOW)

    def review(self, **changes):
        value = dict(status='implemented', progress='verified', summary='Synthetic checks verified this source implementation.',
                     sourceRevision=self.base, checkedAt='2026-10-03T18:00:00Z', links=[])
        value.update(changes)
        return {'schemaVersion': 1, 'features': {'core': value}}

    def pull(self, files, state='OPEN'):
        return [{'number': 7, 'url': sync.GITHUB + '/pull/7', 'state': state, 'files': files}]

    def test_unchanged_baseline_has_fresh_check_but_no_verified_claim(self):
        feed = self.feed()
        self.assertEqual(feed['features']['core']['progress'], 'unchanged')
        self.assertEqual(feed['features']['core']['status'], 'partial')
        self.assertEqual(feed['features']['core']['sourceRevision'], self.base)
        self.assertEqual(feed['checkedAt'], '2026-10-03T19:00:00Z')
        self.assertEqual(feed['sync'], {'mode': 'repository-events', 'intervalMinutes': 60})
        self.assertIn('chat evidence only', feed['features']['conversation']['summary'])

    def test_changed_source_never_upgrades_status_or_leaks_diff(self):
        self.write('app/core.py', 'secret = "ghp_private_fixture"\nemail = "sample@example.invalid"\n')
        current = self.commit()
        feed = self.feed()
        self.assertEqual(feed['repository']['revision'], current)
        self.assertEqual(feed['repository']['branch'], 'codex/complete-text-monkey')
        self.assertEqual(feed['features']['core']['progress'], 'changed')
        self.assertEqual(feed['features']['core']['status'], 'partial')
        self.assertEqual(feed['features']['documentation']['progress'], 'unchanged')
        self.assertNotIn('ghp_private_fixture', json.dumps(feed))
        self.assertNotIn('sample@example.invalid', json.dumps(feed))
        self.assertNotIn(str(self.repo), json.dumps(feed))

    def test_directory_prefix_line_suffix_and_unrelated_paths(self):
        self.write('tests/subdir/new.txt', 'changed\n')
        self.write('app/core.py', 'changed\n')
        self.write('docs/readme.md.extra', 'not same source\n')
        self.write('teststuff/other.txt', 'not under tests\n')
        self.commit()
        rows = self.feed()['features']
        self.assertEqual(rows['suite']['progress'], 'changed')
        self.assertEqual(rows['core']['progress'], 'changed')
        self.assertEqual(rows['documentation']['progress'], 'unchanged')

    def test_uncommitted_edits_do_not_change_fetched_source_status(self):
        self.write('app/core.py', 'uncommitted\n')
        self.assertEqual(self.feed()['features']['core']['progress'], 'unchanged')

    def test_valid_current_review_and_audit_timestamp_are_preserved(self):
        self.write('app/core.py', 'new\n')
        current = self.commit()
        feed = self.feed(self.review(sourceRevision=current))
        row = feed['features']['core']
        self.assertEqual((row['status'], row['progress']), ('implemented', 'verified'))
        self.assertEqual(row['sourceRevision'], current)
        self.assertEqual(row['checkedAt'], '2026-10-03T18:00:00Z')
        self.assertNotEqual(row['checkedAt'], feed['checkedAt'])

    def test_review_survives_only_unrelated_changes(self):
        self.write('docs/readme.md', 'new documentation\n')
        self.commit()
        row = self.feed(self.review())['features']['core']
        self.assertEqual(row['progress'], 'verified')
        self.assertEqual(row['sourceRevision'], self.base)

    def test_stale_review_does_not_upgrade_baseline(self):
        self.write('app/core.py', 'changed\n')
        self.commit()
        row = self.feed(self.review())['features']['core']
        self.assertEqual((row['status'], row['progress']), ('partial', 'changed'))
        self.assertIn('stale', row['summary'])
        self.assertEqual(row['sourceRevision'], self.base)

    def test_divergent_review_is_not_reused_even_with_same_source(self):
        self.git('checkout', '-qb', 'review-branch')
        self.write('unrelated.txt', 'review branch\n')
        reviewed_sha = self.commit()
        self.git('checkout', '--detach', self.base)
        self.write('different.txt', 'current branch\n')
        self.commit()
        row = self.feed(self.review(sourceRevision=reviewed_sha))['features']['core']
        self.assertEqual((row['status'], row['progress']), ('partial', 'changed'))

    def test_invalid_review_id_status_progress_and_commit_are_rejected(self):
        cases = [self.review(status='complete'), self.review(progress='done'),
                 self.review(sourceRevision='f' * 40), self.review(sourceRevision='HEAD'),
                 self.review(checkedAt='2026-10-03T18:00:00'), self.review(checkedAt='2027-01-01T00:00:00Z')]
        unknown = self.review()
        unknown['features']['unknown'] = unknown['features'].pop('core')
        cases.append(unknown)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(sync.FeedError):
                self.feed(data)

    def test_public_notes_and_links_reject_obvious_sensitive_content(self):
        for summary in ['Send to sample@example.invalid', 'Phone +1 202 555 0123', 'secret=hidden',
                        'Bearer private-token', 'Raw\nmessage', '<script>bad</script>', '/Users/sample/private']:
            with self.subTest(summary=summary), self.assertRaises(sync.FeedError):
                self.feed(self.review(summary=summary))
        for url in ['https://github.com/other/repo/pull/1', sync.GITHUB + '/pull/1?token=secret',
                    'https://user:pass@github.com/' + sync.CANONICAL + '/pull/1', 'javascript:alert(1)']:
            with self.subTest(url=url), self.assertRaises(sync.FeedError):
                self.feed(self.review(links=[{'title': 'Review', 'url': url}]))
        row = self.feed(self.review(links=[{'title': 'Review', 'url': sync.GITHUB + '/pull/4'}]))['features']['core']
        self.assertEqual(row['links'][0]['url'], sync.GITHUB + '/pull/4')

    def test_source_paths_and_duplicate_feature_ids_fail_closed(self):
        for path in ['/private/file', '../file', 'app/../file', 'https://example.invalid', 'app\\file']:
            with self.subTest(path=path), self.assertRaises(sync.FeedError):
                sync.source_paths({'sources': [path]})
        self.model['categories'][0]['features'].append(copy.deepcopy(self.model['categories'][0]['features'][0]))
        with self.assertRaises(sync.FeedError):
            self.feed()

    def test_missing_fetched_ref_and_wrong_origin_are_rejected_without_fetch(self):
        self.git('update-ref', '-d', 'refs/remotes/origin/codex/complete-text-monkey')
        with self.assertRaises(sync.FeedError):
            self.feed()
        self.git('update-ref', 'refs/remotes/origin/codex/complete-text-monkey', self.base)
        self.git('remote', 'set-url', 'origin', 'https://github.com/other/repo.git')
        with self.assertRaises(sync.FeedError):
            self.feed()

    def test_open_pr_marks_progress_but_never_implementation(self):
        row = self.feed(pulls=self.pull(['app/core.py']))['features']['core']
        self.assertEqual((row['status'], row['progress']), ('partial', 'in_progress'))
        self.assertIn('Review and merge pending', row['summary'])
        self.assertEqual(row['links'][-1]['url'], sync.GITHUB + '/pull/7')
        self.assertEqual(self.feed(pulls=self.pull(['tests/new.py']))['features']['suite']['progress'], 'in_progress')
        self.assertEqual(self.feed(pulls=self.pull(['app/core.py'], 'MERGED'))['features']['core']['progress'], 'unchanged')

    def test_current_change_and_explicit_blocked_review_outrank_open_pr(self):
        pulls = self.pull(['app/core.py'])
        row = self.feed(self.review(progress='blocked', status='partial'), pulls)['features']['core']
        self.assertEqual(row['progress'], 'blocked')
        self.write('app/core.py', 'current changed\n')
        self.commit()
        self.assertEqual(self.feed(pulls=pulls)['features']['core']['progress'], 'changed')
        self.assertEqual(self.feed(self.review(), pulls)['features']['core']['progress'], 'changed')

    def test_invalid_pr_metadata_rejected_and_no_titles_accepted(self):
        for key, value in [('url', 'https://github.com/other/repo/pull/7'), ('number', True),
                           ('state', 'UNKNOWN'), ('files', ['../private']), ('title', 'Private title')]:
            rows = self.pull(['app/core.py'])
            rows[0][key] = value
            with self.subTest(key=key), self.assertRaises(sync.FeedError):
                self.feed(pulls=rows)

    def test_cli_atomic_output_and_invalid_input_preserves_last_feed(self):
        model = Path(self.temp.name) / 'model.json'
        output = Path(self.temp.name) / 'feed.json'
        reviews = Path(self.temp.name) / 'reviews.json'
        pulls = Path(self.temp.name) / 'pulls.json'
        model.write_text(json.dumps(self.model))
        pulls.write_text(json.dumps(self.pull(['app/core.py'])))
        args = ['--repo', str(self.repo), '--model', str(model), '--output', str(output), '--pull-requests', str(pulls)]
        self.assertEqual(sync.main(args), 0)
        original = output.read_bytes()
        self.assertEqual(json.loads(original)['features']['core']['progress'], 'in_progress')
        reviews.write_text(json.dumps(self.review(status='nonsense')))
        self.assertEqual(sync.main(args + ['--reviews', str(reviews)]), 2)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(sync.main(['--repo', str(self.repo), '--model', str(model), '--output', str(model)]), 2)
        self.assertEqual(list(output.parent.glob('.status-*.tmp')), [])

    def test_malformed_nested_values_fail_without_traceback(self):
        for data in [[], self.review(status=[]), self.review(progress={})]:
            with self.subTest(data=data), self.assertRaises(sync.FeedError):
                self.feed(data)
        for data in [{}, [{'number': 7, 'url': sync.GITHUB + '/pull/7', 'state': [], 'files': []}]]:
            with self.subTest(data=data), self.assertRaises(sync.FeedError):
                self.feed(pulls=data)
        for source in [None, 'app/core.py']:
            with self.subTest(source=source), self.assertRaises(sync.FeedError):
                sync.source_paths({'sources': source})
        with self.assertRaises(sync.FeedError):
            self.feed(self.review(links=[{'title': 'Review', 'url': 'https://[invalid'}]))

    def test_ref_labels_and_review_timestamps_cannot_leak_input(self):
        with self.assertRaises(sync.FeedError):
            sync.build_feed(self.repo, '--help', self.model, now=NOW)
        with self.assertRaises(sync.FeedError):
            self.feed(self.review(checkedAt='invalid timestamp'))


if __name__ == '__main__':
    unittest.main()
