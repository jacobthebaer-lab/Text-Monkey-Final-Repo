import unittest
from unittest.mock import patch
from collect_pr_activity import collect, pr_paths


class ActivityTests(unittest.TestCase):
    def test_only_public_metadata_leaves_collector(self):
        raw = [{'number': 42, 'title': 'Private title', 'body': 'Private body', 'user': {'email': 'private@example.invalid'}, 'patch': 'Private patch'}]
        with patch('collect_pr_activity.gh_pages', return_value=raw) as listing, patch('collect_pr_activity.pr_paths', return_value=['app/core/a.py', 'app/core/a.py']):
            result = collect()
        self.assertEqual(result, [{'number': 42, 'url': 'https://github.com/jacobthebaer-lab/text-monkey/pull/42', 'state': 'OPEN', 'files': ['app/core/a.py']}])
        self.assertIn('base=codex%2Fcomplete-text-monkey', listing.call_args.args[0])

    def test_invalid_number_never_becomes_a_request(self):
        with patch('collect_pr_activity.gh_pages', return_value=[{'number': '../other'}]), patch('collect_pr_activity.pr_paths') as paths:
            with self.assertRaises(ValueError):
                collect()
            paths.assert_not_called()

    def test_graphql_requests_paths_without_patch_or_conversation(self):
        response = type('Response', (), {'returncode': 0, 'stdout': '[{"data":{"repository":{"pullRequest":{"files":{"nodes":[{"path":"app/core/a.py"}]}}}}}]'})()
        with patch('collect_pr_activity.subprocess.run', return_value=response) as run:
            self.assertEqual(pr_paths(42), ['app/core/a.py'])
        arguments = run.call_args.args[0]
        query = next(value for value in arguments if value.startswith('query='))
        self.assertIn('nodes{path}', query)
        self.assertNotIn('patch', query)
        self.assertNotIn('body', query)


if __name__ == '__main__':
    unittest.main()
