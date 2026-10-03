import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from publish_status import publish


class Response(io.BytesIO):
    status = 200


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'status.json'
        self.path.write_text(json.dumps({'schemaVersion': 1, 'features': {'safe-feature': {'status': 'partial'}}}))
        self.url = 'https://text-monkey-universe-dev.example.workers.dev/api/status/publish'
        self.key = 'test-only-placeholder-value-not-a-real-secret'

    def test_credential_only_sent_to_exact_https_status_destination(self):
        for url in ('http://text-monkey-universe-dev.example.workers.dev/api/status/publish',
                    'https://unrelated.workers.dev/api/status/publish',
                    self.url + '?redirect=https://elsewhere.example',
                    self.url.replace('/api/status/publish', '/other')):
            with self.subTest(url=url), patch('publish_status.urlopen') as send:
                with self.assertRaises(ValueError):
                    publish(self.path, url, self.key)
                send.assert_not_called()

    def test_publishes_json_and_checks_acknowledgment(self):
        with patch('publish_status.urlopen', return_value=Response(b'{"ok":true}')) as send:
            self.assertEqual(publish(self.path, self.url, self.key), 1)
            request = send.call_args.args[0]
            self.assertEqual(request.get_method(), 'POST')
            self.assertEqual(request.headers['Authorization'], 'Bearer ' + self.key)
            self.assertEqual(json.loads(request.data)['schemaVersion'], 1)

    def test_malformed_or_unacknowledged_payload_fails(self):
        with patch('publish_status.urlopen', return_value=Response(b'{}')):
            with self.assertRaises(ValueError):
                publish(self.path, self.url, self.key)
        self.path.write_text('{}')
        with patch('publish_status.urlopen') as send:
            with self.assertRaises(ValueError):
                publish(self.path, self.url, self.key)
            send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
